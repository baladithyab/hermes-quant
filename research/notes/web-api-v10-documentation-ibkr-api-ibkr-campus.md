---
title: Web API v1.0 Documentation | IBKR API | IBKR Campus
id: web-api-v10-documentation-ibkr-api-ibkr-campus
tags:
- oms-combo
created: '2026-06-17T20:05:52.702201Z'
updated: '2026-06-17T20:28:22.899615Z'
source: https://ibkrcampus.com/campus/ibkr-api-page/cpapi-v1/#combo-orders
source_domain: ibkrcampus.com
fetched_at: '2026-06-17T20:05:52.702004Z'
fetch_provider: builtin
status: review
type: note
tier: unknown
content_type: unknown
deprecated: false
summary: Web API v1.0 Documentation | IBKR API | IBKR Campus
---

Web API v1.0 Documentation | IBKR API | IBKR Campus
Introduction
Copy Location
Interactive Brokers’ Client Portal Web API delivers real-time access to Interactive Brokers’ trading functionality, including live market data, market scanners, and intra-day portfolio updates. Clients can communicate directly with IBKR infrastructure, both synchronously using HTTP endpoints and in an asynchronous, event-driven manner via websocket. A variety of
authorization and authentication
methods are available to accommodate any use case, including OAuth 1.0a, OAuth 2.0, SSO, and our Java-based CP Gateway tool.
An active Interactive Brokers account is required in order to use the Client Portal API. If you do not already have an account, you can
create one for free
. Please note that you will have to wait for the account to be fully activated before connecting to the API.
The Client Portal API supports only IBKR Pro accounts.
Requirements & Limitations
Copy Location
To access the WebAPI, all accounts must follow a few minimum requirements before data can be received.
Must use an opened IB Account (Demo accounts cannot subscribe to data).
Must use an IBKR PRO account type.
Must maintain a funded account.
Supported Two Factor Authentication (2FA)
Copy Location
Interactive Brokers maintains a strong breadth of supported 2FA systems across our platforms. Given the API does not support account management, certain 2FA methods are not supported. When attempting to authenticate using our API systems, please ensure that a supported 2FA method is enabled for the account.
Two Factor Authentication (2FA) is required for all users at Interactive Brokers.
Supported 2FA Methods
IB Key
Handy Key (Smart Phone applications)
SMS / Text Messages
Digital Security Card+ (DSC+)
Unsupported 2FA Methods
Security Code Card (Sometimes referred to as Bingo Card)
Temporary Security Code Card
Online Code Card
Canadian Residents Restricted From Programmatically Trading Canadian Products
Copy Location
Interactive Brokers Canada Inc. (IBC) does not allow users to use your own trading application to electronically submit order for products traded on a Canadian exchange or other marketplace through API, which would include Third Party Integrations. This decision was made through multiple and extensive communications between IBC compliance and personnel and senior management of the Canadian Investment Regulatory Organization (CIRO), formerly the Investment Industry Regulatory Organization of Canada (IIROC), our self-regulatory organization.
CIRO has implemented
IIROC Dealer Member Rule (DMR) 3200
A. 1. (b) (i) which prohibits CIRO registrants, including IBC, from allowing its clients to use their own automated order systems to generated orders.
Unfortunately, these restrictions would be also applicable with third-party applications like TradingView, NinjaTrader, or other such groups as they use an API connection.
WebAPI Basics Tutorial
Copy Location
Many of our most common features, as well as instructions for installing and running the Client Portal Gateway, are available in our WebAPI Tutorial Series. The series uses Python to implement the WebAPI functionality; however, the requests and response handling would appear the same across languages.
This tutorial covers:
Running the client portal gateway
Requesting Live and Historical Market Data
Placing and Monitoring Orders
Reviewing Individual and Financial Advisor Account Information
Handling Market Scanners
Implementing a Websocket
IBKR’s Client Portal API Tutorial
Client Portal Gateway
Copy Location
For individual accounts looking to access the WebAPI, developers must authenticate using the Client Portal Gateway, a small java program used to route local web requests with appropriate authentication.
Limitations of the Client Portal Gateway
Copy Location
While the vast majority of the endpoints published by Interactive Brokers’ through our WebAPI documentation can be used in both Client Portal Gateway (CPGW) and through OAuth, there are a few unique limitations of the system that should be understood.
Users must log in through the browser on the same machine as Client Portal Gateway in order to authenticate.
All API Endpoint calls must be made on the same machine where the Client Portal Gateway was authenticated.
None of the endpoints beginning with /gw/api, /oauth, or /oauth2 are supported for use in the Client Portal Gateway.
How to Download And Run The Gateway
Copy Location
Download and install the Java Runtime Environment
Copy Location
As the Client Portal API Gateway was developed using Java, a working installation of the Java Runtime Environment (JRE) is required to run it. The minimum required version is Java 8 update 192.
In order to check if you have a working installation of Java, open a terminal and run the following command:
java -version
If Java is installed and correctly configured you should see information about the currently installed version, otherwise an error is raised.
If you do not have a working installation of Java, you can download it
from the official website
Download and unzip the Client Portal API Gateway
Copy Location
Individual clients using Client Portal API will need to use a Java-based API gateway in order to access protected endpoints. The gateway is responsible for routing requests to the backend and ensuring that the brokerage session is authenticated. Interactive Brokers offers both a Standard and Beta release for our Client Portal API platforms. If you ever experience issues with the standard release, please try our Beta client in case your issue has already been resolved.
Download the Standard Release
Download the Beta Release
Run the API Gateway
Copy Location
In order to launch the Client Portal Gateway, you must execute a set of commands through the Windows Command Prompt or Unix Terminal. This can not be done through the default file explorer. The API gateway is meant to be run on a local machine. As such, attempting to operate the gateway on a separate machine from where commands are generated may result in the issues and is not a supported practice by Interactive Brokers.
By default, the gateway will listen on port 5000. Clients can however change this to any available port on their device by modifying the listenPort field in the gateway configuration file ‘conf.yaml’, found in the ‘root’ directory of the gateway.
After successfully running the gateway, you may proceed through the
Authentication
steps before proceeding to calling your requested endpoints.
Using the terminal, Find and open the unzipped Client Portal Gateway directory.
For example, assume my Client Portal Gateway directory is in the Windows
C:/Users/Example/Downloads/
, then I can run the following command to change to the directory:
cd C:/Users/Example/Downloads/clientportal.gw
After that, on Windows, launch the gateway using the following command:
bin\run.bat root\conf.yaml
And in the case of Unix systems:
bin/run.sh root/conf.yaml
How To Modify The Client Portal Gateway Port
Copy Location
In some cases, a user may need to modify the port for their Client Portal Gateway. This most often arises because another process is already occupying Localhost port 5000.
To modify your port:
(Optional) Close the Client Portal Gateway if it is already running
Navigate to your Client Portal Gateway directory, such as /Users/my_user/cpgw
Enter the ./root directory to edit your conf.yaml file
Modify “listenPort” on line 4 from “5000” to the value of your choice.
Port “5001” is a standard alternative that is not typically in use.
Save the content of the file and launch the Client Portal Gateway
ip2loc: "US"
proxyRemoteSsl: true
proxyRemoteHost: "https://api.ibkr.com"
listenPort: 5001
listenSsl: true
ccp: false
svcEnvironment: "v1"
sslCert: "vertx.jks"
sslPwd: "mywebapi"
authDelay: 3000
portalBaseURL: ""
serverOptions:
    blockedThreadCheckInterval: 1000000
    eventLoopPoolSize: 20
    workerPoolSize: 20
    maxWorkerExecuteTime: 100
    internalBlockingPoolSize: 20
webApps:
    - name: "demo"
      index: "index.html"
cors:
    origin.allowed: "*"
    allowCredentials: false
ips:
  allow:
    - 192.*
  deny:
Client Portal Gateway FAQ
Copy Location
Copy Location
Q:
Why is my browser saying I have an insecure connection? Why are my requests being rejected because of an invalid SSL certificate?
A:
When navigating to the Client Portal API Gateway login page, you may see a warning from your browser regarding a missing valid SSL certificate. This is expected. The API gateway does not come bundled with a valid certificate and it is up to the user to install one signed by themselves.
Note:
It is important to note that the connection is only insecure between the user to their own localhost. In other words, only the connection on the local computer is insecure. However, requests sent from the locahost to Interactive Brokers will maintain a secure connection.
Copy Location
Q:
Can I automate the Client Portal API Gateway authentication process?
A:
There is currently no mechanism available on Interactive Brokers’ end to permit individual clients to automate the brokerage session authentication process when using Client Portal API. Interactive Brokers does not recommend the use of third-party solutions to establish a brokerage session. This can put your account at risk from potentially malicious projects.
Note:
Interactive Brokers is unable to provide support for third-party wrappers.
Copy Location
Q:
How often do I need to log in through the browser while using the Client Portal Gateway?
A:
Clients must reauthenticate using the Client Portal Gateway at least once after midnight each day.
Copy Location
Q:
Why did I received "Server listen failed Address already in use" on my Mac when running the Client Portal Gateway?
A:
This is a known process which operates on MacOS computers which operates on the localhost:5000 port. To resolve this, you will need to Modify The Port Of Client Portal Gateway.
Authentication
Copy Location
After launching the client portal gateway, navigate to localhost:5000 and sign in using your standard Interactive Brokers credentials. After entering your username and password, you will be prompted for Two-Factor Authentication based on your authentication method of choice. After entering your information, you will see
Client login succeeds
to indicate a successful login.
If you are signing in with your paper account, be sure to use the unique Paper Trading username. This can be found by logging in to the
Client Portal
with your live account’s username then selecting the Head and Shoulders icon in the top right corner and click Settings. Here, you should see a link for Paper Trading account. This will list your Paper Account’s account ID and username.
Session Authentication
Copy Location
An authenticated brokerage session is necessary to access order information, place orders, financial advisor information, or receive market data via Client Portal API, and generally across all /iserver endpoints. Individual clients using Client Portal API are required to use the API Gateway in order to establish a secure brokerage session.
Interactive Brokers permits a single username to be signed in once at any given time. However, the Client Portal API permits users to log in without connecting to their brokerage session. This allows brokerage sessions to continue trading while non-brokerage sessions can perform certain actions such as requesting portfolio information without breaking existing trading sessions.
Background regarding CP Web API sessions:
Copy Location
An IB username can only have one brokerage session (trading-enabled) session open at a time, i.e., it can only trade and use similarly restricted functionality on one platform (TWS, Client Portal, etc.) at any given moment, and switching to trading on a different platform entails closing the existing brokerage session and opening a new one from the new platform.
Web API sessions in general are two-tiered:
An “outer” prerequisite “read-only session” that is required to be active/valid in order to make any CP Web API request, though by itself it only permits access to
non-/iserver
endpoints.
The “brokerage session”, established after the read-only, that permits access to trading, consumption of market data, and all other functionality behind
/iserver
endpoints.
This two-tiered arrangement reflects the way our Client Portal website permits you to log into a read-only session for account management when that username is already logged into a brokerage session elsewhere (e.g., TWS), leaving the TWS brokerage session undisturbed.
The CPAPI iframe actually behaves like the CP Gateway mentioned frequently in our CP Web API documentation (the Java-based reverse proxy tool for retail clients). The CPAPI iframe will attempt to establish a brokerage session automatically, as soon as a login occurs and the read-only session is created via SSO.
Understanding Brokerage Sessions
Copy Location
All resources behind /iserver are accessible only with an active “brokerage” session. Some additional info:
TWS being a trading platform requires that a username has trading permissions in order for it to be used to access TWS – there is no purely “read-only” TWS access
The Client Portal website, on the other hand, contains all of the reporting and account management functionality, and consequently it is possible to log in to Client Portal with a read-only/no-trading-permissions username and access reports, portfolio info, etc.
Iserver is effectively TWS running on IB infrastructure, and it serves all of the trading-permissions-required resources, hence the need for a brokerage session to access those endpoints
As a result of #2 and #3, the CP Web API also has this two-tiered session arrangement: The first tier is the read-only Portal session, and the second tier is the brokerage session through which you can talk to /iserver and actually trade the account(s), etc.
After logging in with OAuth without competing sessions, you will have a Portal session and can visit non-iserver resources. The additional
/iserver/auth/ssodh/init
endpoint is used to subsequently reopen a brokerage session with our backend, through which you can access the protected /iserver endpoints.
The Client Portal Gateway will automatically initialize the brokerage session.
The brokerage session is associated with the credentials in use – your username – so you don’t need to select an account here. Rather, once you have access to a brokerage session, you can manipulate all accounts visible/accessible to the username in use.
Non-iserver endpoints like /portfolio are served by different backend processes that do not require trading permissions and are accessible without a brokerage session
Understanding Interactive Brokers terminology
Copy Location
A brokerage session (or trading session) is established by a username (your credentials), which in turn has trading permissions for one or more accounts (the actual pools of equity). A single username can only have one trading (or “brokerage”) session active at a time. Permissions for trading in general/ for specific asset classes, market data subscriptions (and thus access to the subscribed feeds), etc. are carried by usernames, not the underlying accounts. Hence references to brokerage sessions refer to a logged-in username that is in contact with IBKR’s backend trading infrastructure.
Managing Multiple Sessions
Copy Location
Only a single active brokerage session can exist for any username accross all IBKR services. If you are logged in to either Client Portal, TWS, or IBKR Mobile, make sure to log out and try reauthenticating your session Client Portal API again.
Clients wishing to use multiple IBKR products at the same time (TWS, IBKR Mobile or Client Portal) can do so by creating a new username that can then be used to log into other services while using the Client Portal API. To create a second username please see the following
IBKR Knowledege Base
article.
Note:
In accordance with market data vendor requirements, market data services are user-specific and any username subscribed will be assessed a separate market data subscription fee.
Using a Paper Account
Copy Location
Customers are encouraged to authenticate and test with their Paper accounts before proceeding to live testing in the Client Portal API. Users may notice that unlike other parts of Interactive Brokers, there is no slider to indicate a live or Paper account login. As such, customers must use their specific Paper username to authenticate. You can find your Paper username by following these steps:
Log in to the
Client Portal
Select the Head & Shoulders Icon
Click “Settings”.
On the left under “Account Configuration” select “Paper Trading Account”
You should see a Paper Trading Username, Paper Trading Account Number, as well as options for linking your Market Data.
If you do not know your password already, it is recommended to select “Reset Paper Trading Password” to have a unique password for paper trading. This can be reset at any time.
You may now log in to the Client Portal Gateway using your newfound Paper Trading username and password.
Authentication Frequently Asked Questions
Copy Location
Copy Location
Q:
How can I tell if my brokerage session is authenticated?
A:
The endpoint
iserver/auth/status
can be used to determine the current authentication status of the session. Once you have logged in using the Client Portal API Gateway, you can make a request to this endpoint to determine if your session is fully authenticated. If the session is fully authenticated, the response from this endpoint will display
"authenticated": true
.
Copy Location
Q:
How long does a session remain authenticated?
A:
A session can remain authenticated for up to 24 hours, resetting at midnight for New York, U.S.; Zug, Switzerland; or Hong Kong time depending on your nearest connection.
Sessions will time out after approximately 6 minutes without sending new requests or maintaining the
/tickle endpoint
at least every 5 minutes.
Daily maintenance of IBKR’s servers could result in a disconnect earlier than the 24 hour period mentioned above. We advise disconnecting your session from your gateway and restarting it after the maintenance time to minimize any potential problems that may arise. Information on server reset times and system status updates can be found on the
System Staus
page.
Copy Location
Q:
How can I prevent the session from timing out?
A:
A Client Portal API brokerage session will timeout if no requests are received within a period of 5 minutes. In order to prevent the session from timing out, the endpoint
/tickle
should be called on a regular basis. It is recommended to call this endpoint approximately every minute.
If the brokerage session has timed out but the session is still connected to the IBKR backend, the response to
/auth/status
returns ‘connected’:true and ‘authenticated’:false. Calling the
/iserver/auth/ssodh/init
endpoint will initialize a new brokerage session.
Copy Location
Q:
Is it possible to authenticate a live brokerage session without the use of a Two Factor Authentication (2FA) device?
A:
The login process to the Client Portal API Gateway is the same as to Client Portal. As the Client Portal has access to sensitive information and banking functionalities, two-factor authentication is mandatory for login.
Pacing Limitations
Copy Location
Interactive Brokers has implemented pacing limits on endpoints accessible via Client Portal API.
There is a global limit of 10 total requests per second.
Some endpoint specific limits are also in place. These limits can be found in the table below.
Any endpoint not listed in the table below follows the global restriction of 10 requests per second.
Where this limit is exceeded, the API will return a “429 Too Many Requests” exception. Violator IP addresses are put in a penalty box for 15 minutes. After this period, the IP address is removed from the penalty box until another request exceeds the limit again. Repeat violator IP addresses can be permanently blocked until the issue is resolved.
Endpoint
Method
Limit
/fyi/unreadnumber
GET
1 req/sec
/fyi/settings
GET
1 req/sec
/fyi/settings/{typecode}
POST
1 req/sec
/fyi/disclaimer/{typecode}
GET
1 req/sec
/fyi/disclaimer/{typecode}
PUT
1 req/sec
/fyi/deliveryoptions
GET
1 req/sec
/fyi/deliveryoptions/email
PUT
1 req/sec
/fyi/deliveryoptions/device
POST
1 req/sec
/fyi/deliveryoptions/{deviceId}
DELETE
1 req/sec
/fyi/notifications
GET
1 req/sec
/fyi/notifications/more
GET
1 req/sec
/fyi/notifications/{notificationId}
PUT
1 req/sec
/iserver/account/orders
GET
1 req/5 secs
/iserver/account/pnl/partitioned
GET
1 req/5 secs
/iserver/account/trades
GET
1 req/5 secs
/iserver/marketdata/history
GET
5 concurrent requests
/iserver/marketdata/snapshot
GET
10 req/s
/iserver/scanner/params
GET
1 req/15 mins
/iserver/scanner/run
POST
1 req/sec
/pa/performance
POST
1 req/15 mins
/pa/summary
POST
1 req/15 mins
/pa/transactions
POST
1 req/15 mins
/portfolio/accounts
GET
1 req/5 secs
/portfolio/subaccounts
GET
1 req/5 secs
/sso/validate
GET
1 req/min
/tickle
GET
1 req/sec
Regular Server Maintenance
Copy Location
Interactive Brokers maintains regularly scheduled maintenance for all users of the Client Portal API. The system reset time is unique from that of the standard Trader Workstation connection. Clients will see disconnects at midnight of their connecting region.
Server Reset Times
North America
Europe
Asia
Local Time
00:00 US/Eastern
00:00 CEST
00:00 HKT
Universal Time Coordinated
20:00 UTC
02:00 UTC
08:00 UTC
Endpoints
Copy Location
To make calls to create or modify data to Interactive Brokers, users must use an URL endpoint through the localhost. Each call is comprised of a base URL and an endpoint.
The base url for the
Client Portal Gateway
is:
https://localhost:5000/v1/api
By default, the Client Portal Gateway is not bundled with a signed certificate. As such, customers should either look to independently have their certificate signed, or submit requests to their localhost as ‘insecure’.
OAuth 1.0a users should route requests to
https://api.ibkr.com/v1/api
instead.
Python
cURL
Python web requests are displayed with various external libraries that users may wish to implement. While this documentation is built around the
requests
library, there are a few additional libraries to consider:
requests
json
websocket-client
To send ‘insecure’ requests in python, add “verify=False” as a request argument.
Please note that cURL requests are formatted using the cURL standard,
documented here
. Some OS specific platforms may be unique, and will require some adjustment.
Our system displays the default bash structure.
Unix and bash use \ (backslash)
Powershell will utlize ` (backtick)
Command Prompt uses ^ (caret)
To send ‘insecure’ requests in cURL, add “–insecure” into the request.
Headers
All requests should include the following Headers:
Host: This should be set to “api.ibkr.com”
User-Agent: This may be set to anything, though it is best to reference your environment or pull directly from the browser.
Accept: This should be set to “*/*” to indicate any return format. Typically application/json is returned, though this is not always the case.
Connection: This should be set to “keep-alive”.
Content-Length: Sending a POST request must include Content-Length. Requests sent without Content-Length will return a 411 error.
Note: Most languages pass this information by default. However, some implementations, such as Java Springboot, may not include these headers in a standard request.
Alerts
Copy Location
Alerts allow users to set up notifications to pop-up in their Trader Workstation, Interactive Brokers app, or via email in the event of a particular event
Create or Modify Alert
Copy Location
Endpoint used to create a new alert, or modify an existing alert.
POST /iserver/account/{{ accountId }}/alert
Path Params
accountId:
String.
Required
Identifier for the unique account to retrieve information from.
Value Format: “DU1234567”
Body Prams
alertName
:
String.
Required
Used as a human-readable identifier for your created alert.
Format Structure: “Alert Name”
alertMessage
:
String.
Required
The body content of what your alert will report once triggered
Value Format: “MESSAGE TEXT”
alertRepeatable
:
int.
Required
Boolean number (0, 1) signifies if an alert can be triggered more than once.
A value of ‘1’ is required for MTA alerts
Value Format:
email
:
String
.
Required if ‘sendMessage’ == 1
Email address you want to send email alerts to
Value Format:
expireTime
:
String
.
Required if ‘tif’ == ‘GTD’
Used with a tif of “GTD” only. Signifies time when the alert should terminate if no alert is triggered.
Value Format: “YYYYMMDD-HH:mm:ss”
iTWSOrdersOnly
:
int.
Optional
Boolean number (0, 1) to allow alerts to trigger alerts through the mobile app.
Value Format: 1
outsideRth
:
int.
Required
Boolean number (0, 1) to allow the alert to trigger outside of regular trading hours.
Value Format: 1
sendMessage
:
int.
Optional
Boolean number (0, 1) to allow alerts to trigger email messages
Value Format: 1
showPopup
:
int.
Optional
Boolean number (0, 1) to allow alerts to trigger TWS Pop-up messages
Value Format: 1
tif
:
String..
Required
Time in Force duration of alert. Allowed: [“GTC”, “GTD”]
Value Format: “DAY”
conditions
:
List of Arrays
.
Required
Container for all conditions applied for an alert to trigger.
Required field.
Value Format:[ {…} ]
conidex
:
String.
Required
Concatenation of conid and exchange. Formatted as “conid@exchange”
Value Format: “265598@SMART”
logicBind
:
String.
Required
Describes how multiple conditions should behave together.
Allowed values are: {“a”: “AND”, “o”: “OR”, “n”: “END”}
Value Format: “a”
operator
:
String.
Required
Indicates whether the trigger should be above or below the given value.
Value Format:”>=”
timeZone
:
String.
Required for MTA alerts
Only needed for some MTA alert condition
Value Format: “US/Eastern”
triggerMethod
:
String.
Required
Pass the string representation of zero, “0”
Value Format: “0”
type
:
int.
Required
Designate what condition type to use.
Allowed values: {1: Price, 3: Time, 4: Margin, 5: Trade, 6: Volume, 7: MTA market, 8: MTA Position, 9: MTA Account Daily PnL}
Value Format: 1
value
:
String.
Required
Trigger value based on Type. Allows a default value of “*”.
Value Format: “195.00”, “YYYYMMDD-HH:mm:ss”
}
]
Python
cURL
request_url = f"{baseUrl}/iserver/account/U1234567/alert"
json_content = {
  "alertMessage": "AAPL Price Drop!",
  "alertName": "AAPL_Price",
  "expireTime":"20270101-12:00:00",
  "alertRepeatable": 0,
  "outsideRth": 0,
  "sendMessage": 1,
  "email": "user@domain.net",
  "iTWSOrdersOnly": 0,
  "showPopup": 0,
  "tif": "GTD",
  "conditions": [{
    "conidex": "265598@SMART",
    "logicBind": "n",
    "operator": "<=",
    "triggerMethod": 0,
    "type": 1,
    "value": "183.34"
  }]
}
requests.post(url=request_url, json=json_content)
curl \
--url {{baseUrl}}/iserver/account/U1234567/alert \
--request POST \
--header 'Content-Type:application/json' \
--data '{
  "alertMessage": "AAPL Price Drop!",
  "alertName": "AAPL_Price",
  "expireTime":"20231231-12:00:00",
  "alertRepeatable": 0,
  "outsideRth": 0,
  "sendMessage": 0,
  "email": "user@domain.net",
  "iTWSOrdersOnly": 0,
  "showPopup": 0,
  "tif": "GTD",
  "conditions": [{
    "conidex": "265598@SMART",
    "logicBind": "n",
    "operator": "<=,
    "triggerMethod": 0,
    "type": 1,
    "value": "183.34"
  }]
}'
Response Object:
Returns a single json object
request_id: integer.
Always returns ‘null’
order_id: integer.
Signifies tracking ID for given alert.
success: boolean.
Displays result status of alert request
text: String.
Response message to clarify success status reason.
order_status: String.
Returns ‘null’
warning_message: String.
Returns ‘null’
}
{
  "request_id": null,
  "order_id": 9876543210,
  "success": true,
  "text": "Submitted",
  "order_status": null,
  "warning_message": null
}
Get a list of available alerts
Copy Location
Retrieve a list of all alerts attached to the provided account.
GET /iserver/account/{{ accountId }}/alerts
Path Parameters
accountId:
String
.
Required
Identifier for the unique account to retrieve information from.
Value Format: “DU1234567”
Python
cURL
request_url = f"{baseUrl}/iserver/account/U1234567/alerts"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/iserver/account/U1234567/alerts \
--request GET
Response Object:
Returns an array of comma-separated json objects
order_id:
int.
The searchable order ID
account:
String
.
The account the alert was attributed to.
alert_name:
String.
The requested name for the alert.
alert_active:
int.
Determines if the alert is active or not
order_time:
String.
UTC-formatted time of the alert’s creation.
alert_triggered:
bool.
Confirms if the order is is triggered or not.
alert_repeatable:
int.
Confirms if the alert is enabled to repeat.
[
  {
    "order_id": 9876543210,
    "account": "U1234567",
    "alert_name": "AAPL Price",
    "alert_active": 1,
    "order_time": "20231211-18:55:35",
    "alert_triggered": false,
    "alert_repeatable": 0
  }
]
Get details of a specific alert
Copy Location
Request details of a specific alert by providing the assigned order ID.
GET /iserver/account/alert/{{ order_id }}
Path Parameters
order_id:
int
.
Required
Alert ID returned from the original alert creation, or from the list of available alerts.
Query Parameters
type:
String
.
Required
Must always pass ‘Q’.
Python
cURL
request_url = f"{baseUrl}/iserver/account/alert/9876543210?type=Q"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/iserver/account/alert/9876543210?type=Q \
--request GET
Response Object
account:
String.
Requestor’s account ID
order_id:
int.
Alert’s tracking ID. Can be used for modifying or deleting alerts.
alertName:
String.
Human readable name of the alert.
tif:
String.
Time in Force effective for the Alert
expire_time:
String.
Returns the UTC formatted date used in GTD orders.
alert_active:
int.
Returns if the alert is active or disabled.
alert_repeatable:
int.
Returns if the alert can be sent more than once.
alert_email:
String.
Returns the designated email address for sendMessage functionality.
alert_send_message:
int.
Returns whether or not the alert will send an email.
alert_message:
String.
Returns the body content of what your alert will report once triggered
alert_show_popup:
int.
Returns whether or not the alert will trigger TWS Pop-up messages
alert_play_audio:
int.
Returns whether or not the alert will play audio
order_status:
String.
Always returns “Presubmitted”.
alert_triggered:
int.
Returns whether or not the alert was triggered yet.
fg_color:
String.
Always returns “#FFFFFF”. Can be ignored.
bg_color:
String.
Always returns “#000000”. Can be ignored.
order_not_editable:
bool.
Returns if the order can be edited.
itws_orders_only:
int.
Returns whether or not the alert will trigger mobile notifications.
alert_mta_currency:
String.
Returns currency set for MTA alerts. Only valid for alert type 8 & 9.
alert_mta_defaults:
String.
Returns current MTA default values.
tool_id:
int.
Tracking ID for MTA alerts only. Returns ‘null’ for standard alerts.
time_zone:
String.
Returned for time-specifc conditions.
alert_default_type:
int.
Returns default type set for alerts. Configured in Client Portal.
condition_size:
int.
Returns the total number of conditions in the alert.
condition_outside_rth:
int.
Returns whether or not the alert will trigger outside of regular trading hours.
conditions:
Array of json objects.
Returns all conditions, formatted as [ {Condition1}, {Condition2}, {…} ]
condition_type:
int.
Returns the type of condition set.
conidex:
String.
Returns full conidex in the format “conid@exchange”
contract_description_1:
String.
Includes relevant descriptions (if applicable).
condition_operator:
String.
Returns condition set for alert.
condition_trigger_method:
int.
Returns triggerMethod value set.
condition_value:
String.
Returns value set.
condition_logic_bind:
String
Returns logic_bind value set.
condition_time_zone:
Returns timeZone value set.
{
  "account": "U1234567",
  "order_id": 9876543210,
  "alert_name": "AAPL Price",
  "tif": "GTD",
  "expire_time": "20231231-12:00:00",
  "alert_active": 1,
  "alert_repeatable": 0,
  "alert_email": null,
  "alert_send_message": 0,
  "alert_message": "MTA TEST!",
  "alert_show_popup": 0,
  "alert_play_audio": null,
  "order_status": "Submitted",
  "alert_triggered": false,
  "fg_color": "#FFFFFF",
  "bg_color": "#0000CC",
  "order_not_editable": false,
  "itws_orders_only": 0,
  "alert_mta_currency": null,
  "alert_mta_defaults": null,
  "tool_id": null,
  "time_zone": null,
  "alert_default_type": null,
  "condition_size": 1,
  "condition_outside_rth": 0,
  "conditions": [
    {
      "condition_type": 1,
      "conidex": "265598@SMART",
      "contract_description_1": "AAPL",
      "condition_operator": "<=",
      "condition_trigger_method": "0",
      "condition_value": "183.34",
      "condition_logic_bind": "n",
      "condition_time_zone": null
    }
  ]
}
Get MTA Alert
Copy Location
Retrieve information about your MTA alert.
Each login user only has one mobile trading assistant (MTA) alert with it’s own unique tool id that cannot be changed.
MTA alerts can not be created or deleted, only modified. When modified a new order Id is generated.
See
here
for more information on MTA alerts.
GET /iserver/account/mta
Request Object
No additional parameters necessary.
Python
cURL
request_url = f"{baseUrl}/iserver/account/mta"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/iserver/account/mta \
--request GET
Response Object
account:
String.
Requestor’s account ID
order_id:
int.
Alert’s tracking ID. Can be used for modifying or deleting alerts.
alertName:
String.
Human readable name of the alert.
tif:
String.
Time in Force effective for the Alert
expire_time:
String.
Returns the UTC formatted date used in GTD orders.
alert_active:
int.
Returns if the alert is active or disabled.
alert_repeatable:
int.
Returns if the alert can be sent more than once.
alert_email:
String.
Returns the designated email address for sendMessage functionality.
alert_send_message:
int.
Returns whether or not the alert will send an email.
alert_message:
String.
Returns the body content of what your alert will report once triggered
alert_show_popup:
int.
Returns whether or not the alert will trigger TWS Pop-up messages
alert_play_audio:
int.
Returns whether or not the alert will play audio
order_status:
String.
Always returns “Presubmitted”.
alert_triggered:
int.
Returns whether or not the alert was triggered yet.
fg_color:
String.
Always returns “#FFFFFF”. Can be ignored.
bg_color:
String.
Always returns “#000000”. Can be ignored.
order_not_editable:
bool.
Returns if the order can be edited.
itws_orders_only:
int.
Returns whether or not the alert will trigger mobile notifications.
alert_mta_currency:
String.
Returns currency set for MTA alerts. Only valid for alert type 8 & 9.
alert_mta_defaults:
String.
Returns current MTA default values.
tool_id:
int.
Tracking ID for MTA alerts only. Returns ‘null’ for standard alerts.
time_zone:
String.
Returned for time-specifc conditions.
alert_default_type:
int.
Returns default type set for alerts. Configured in Client Portal.
condition_size:
int.
Returns the total number of conditions in the alert.
condition_outside_rth:
int.
Returns whether or not the alert will trigger outside of regular trading hours.
conditions:
Array of json objects.
Returns all conditions, formatted as [ {Condition1}, {Condition2}, {…} ]
condition_type:
int.
Returns the type of condition set.
conidex:
String.
Returns full conidex in the format “conid@exchange”
contract_description_1:
String.
Includes relevant descriptions (if applicable).
condition_operator:
String.
Returns condition set for alert.
condition_trigger_method:
int.
Returns triggerMethod value set.
condition_value:
String.
Returns value set.
condition_logic_bind:
String
Returns logic_bind value set.
condition_time_zone:
Returns timeZone value set.
{
  "account": "U1234567",
  "order_id": 9998887776,
  "alert_name": null,
  "tif": "GTC",
  "expire_time": null,
  "alert_active": 1,
  "alert_repeatable": 1,
  "alert_email": null,
  "alert_send_message": 1,
  "alert_message": null,
  "alert_show_popup": 0,
  "alert_play_audio": null,
  "order_status": "Inactive",
  "alert_triggered": false,
  "fg_color": "#000000",
  "bg_color": "#AFAFAF",
  "order_not_editable": false,
  "itws_orders_only": 0,
  "alert_mta_currency": "USD",
  "alert_mta_defaults": "9:STATE=1,MIN=-43115000,MAX=43115000,STEP=500,DEF_MIN=-4311500,DEF_MAX=4311500|{{...}}",
  "tool_id": 55834574848,
  "time_zone": "GMT (GMT),GMT (Africa/Abidjan),{{...}}",
  "alert_default_type": null,
  "condition_size": 0,
  "condition_outside_rth": 0,
  "conditions": []
}
Activate or deactivate an alert
Copy Location
Activate or Deactivate existing alerts created for this account. This does not delete alerts, but disables notifications until reactivated.
POST /iserver/account/{{ accountId }}/alert/activate
Request Details
Path Parameters
accountId:
String
.
Required
Identifier for the unique account to retrieve information from.
Value Format: “DU1234567”
Request Body
alertId
:
int.
Required
The alertId, or order_id, received from order creation or the list of alerts.
alertActive
:
int.
Required
Set whether or not the alert should be active (1) or inactive (0)
Python
cURL
request_url = f"{baseUrl}/iserver/account/U1234567/alert/activate"
--request POST \
--header 'Content-Type:application/json' \
--data '{
    "alertId": 9876543210,
    "alertActive": 1
}'
curl \
--url {{baseUrl}}/iserver/account/U1234567/alert/activate \
--request POST \
--header 'Content-Type:application/json' \
--data '{
    "alertId": 9876543210,
    "alertActive": 1
}'
Response Object
request_id:
int
.
Returns ‘null’
order_id:
int
.
Returns requested alertId or order_id
success:
bool
.
Returns true if successful
text:
String
.
Adds additional information for “success” status.
failure_list:
String
.
If “success” returns false, will list failed order Ids
{
  "request_id": null,
  "order_id": 9876543210,
  "success": true,
  "text": "Request was submitted",
  "failure_list": null
}
Delete an alert
Copy Location
Permanently delete an existing alert.
If alertId is 0, it will delete all alerts
If you call delete an MTA alert, it will reset to the default state.
DELETE /iserver/account/{{ accountId }}/alert/{{ alertId }}
Request Parameters
Path Parameters
accountId:
String
.
Required
Identifier for the unique account to retrieve information from.
Value Format: “DU1234567”
alertId:
int
.
Required
order_id returned from the original alert creation, or from the list of available alerts.
Python
cURL
request_url = f"{baseUrl}/iserver/account/U1234567/alert/9876543210"
json_content = {}
requests.delete(url=request_url, json=json_content)
curl \
--url {{baseUrl}}/iserver/account/U1234567/alert/9876543210 \
--request DELETE
Response Object
request_id:
int
.
Returns ‘null’
order_id:
int
.
Returns requested alertId or order_id
success:
bool
.
Returns true if successful
text:
String
.
Adds additional information for “success” status.
failure_list:
String
.
If “success” returns false, will list failed order Ids
{
  "request_id": null,
  "order_id": 9876543210,
  "success": true,
  "text": "Request was submitted",
  "failure_list": null
}
Accounts
Copy Location
Account Profit and Loss
Copy Location
Returns an object containing PnL for the selected account and its models (if any).
GET /iserver/account/pnl/partitioned
Request Object:
No additional parameters necessary.
Python
cURL
request_url = f"{baseUrl}/iserver/account/pnl/partitioned"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/iserver/account/pnl/partitioned \
--request GET
Response Object:
upnl
: JSON Object.
Refers to “updated PnL”. Holds a json object of key-value paired account pnl details.
{accountId}.Core:
JSON Object.
An object based on your current account or group model.
rowType:
int.
Returns the positional value of the returned account. Always returns 1 for individual accounts.
dpl:
float.
Daily PnL for the specified account profile.
nl:
float.
Net Liquidity for the specified account profile.
upl:
float.
Unrealized PnL for the specified account profile.
el:
float.
Excess Liquidity for the specified account profile.
mv:
float.
Margin value for the specified account profile.
{
  "upnl": {
    "U1234567.Core": {
      "rowType": 1,
      "dpl": 15.7,
      "nl": 10000.0,
      "upl": 607.0,
      "el": 10000.0,
      "mv": 0.0
    }
  }
}
Search Dynamic Account
Copy Location
Broker accounts configured with the DYNACCT property will not receive account information at login. Instead, they must dynamically query then set their account number.
Important:
This will not function for individual or financial advisor accounts. This will only be functional for IBrokers with the DYNACCT property approved.
Customers without the DYNACCT property will receive the following message
{
    "error": "Details currently unavailable. Please try again later and contact client services if the issue persists.",
    "statusCode": 503
}
Returns a list of accounts matching a query pattern set in the request.
GET /iserver/account/search/{{ searchPattern }}
Request Object
Query Params
searchPattern:
String.
Required
The pattern used to describe credentials to search for.
Valid Format: “DU” in order to query all paper accounts.
Python
cURL
request_url 
 f"{baseUrl}/iserver/account/search/U123"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/iserver/account/search/U123 \
--request GET
Response Object
matchedAccounts:
List of objects.
Contains a series of objects that pertain to the account information requested.
[{
accountId:
String.
Returns a matching account ID that corresponds to the matching value.
alias:
String.
Returns the corresponding alias or alternative name for the specific account ID. May be a duplicate of the accountId value in most cases.
allocationId:
String.
Returns the allocation identifier used internally for the account.
}]
pattern:
String.
Displays the searchPattern used for the request.
{
  "matchedAccounts": [
    {
      "accountId": "U1234567",
      "alias": "U1234567",
      "allocationId": "1"
    }
  ],
  "pattern":"U123"
}
Set Dynamic Account
Copy Location
Broker accounts configured with the DYNACCT property will not receive account information at login. Instead, they must dynamically query then set their account number.
Important:
This will not function for individual or financial advisor accounts. This will only be functional for IBrokers with the DYNACCT property approved.
Customers without the DYNACCT property will receive the following message
{
    "error": "Details currently unavailable. Please try again later and contact client services if the issue persists.",
    "statusCode": 503
}
Set the active dynamic account. Values retrieved from
Search Dynamic Account
POST /iserver/dynaccount
Request Object
Body Params
acctId:
String.
Required
The account ID that should be set for future requests.
Python
cURL
request_url = f"{baseUrl}/iserver/dynaccount"
json_content = {
  "acctId": "U1234567
}
requests.post(url=request_url, json=json_content)
curl \
--url {{baseUrl}}/iserver/dynaccount \
--request POST \
--header 'Content-Type:application/json' \
--data '{
  "acctId": "U1234567
}'
Response Object
set:
bool.
Confirms if the account change was fully set.
acctId:
String.
The account ID that was set for future use.
{
  "set": "true",
  "acctId": "U1234567",
}
Signatures and Owners
Copy Location
Receive a list of all applicant names on the account and for which account and entity is represented.
GET /acesws/{{ accountID }}/signatures-and-owners
Request Object
Path Params
accountId:
String.
Required
Pass the account identifier to receive information for.
Valid Structure: “U1234567”
Python
cURL
request_url = f"{baseUrl}/acesws/U1234567/signatures-and-owners"
request.get(url=request_url)
curl \
--url {{baseUrl}}/acesws/U1234567/signatures-and-owners \
--request GET
Response Object
accountId:
String.
Specified account identifier in the request.
users:
Array of Objects.
Returns all usernames and their information affiliated with the account.
[{
roleId:
String.
Returns the role of the username as it relates to the account.
hasRightCodeInd:
bool.
Internal use only.
username:
String.
Returns the username for the particular user under the account.
entity:
Object.
Provides information about the particular entity.
{
firstName:
String.
Returns the first name of the user.
lastName:
String.
Returns the last name of the user.
entityType:
String.
Returns the type of entity assigned to the user.
Valid Value: “INDIVIDUAL”, “Joint”, “ORG”
entityName:
String.
Returns the full entity’s name, concatenating the first and last name fields.
}}]
applicant:
Object.
Provides information about the individual listed for the account.
{
signatures:
Array of Strings.
Returns all names attached to the account.
}
{
  "accountId": "U1234567",
  "users": [
    {
      "roleId": "OWNER",
      "hasRightCodeInd": true,
      "userName": "user1234",
      "entity": {
        "firstName": "John",
        "lastName": "Smith",
        "entityType": "INDIVIDUAL",
        "entityName": "John Smith"
      }
    },
    {
      "roleId": "Trustee",
      "hasRightCodeInd": False,
      "userName": "user5678",
      "entity": {
        "firstName": "Jane",
        "lastName": "Doe",
        "entityType": "INDIVIDUAL",
        "entityName": "Jane Doe"
      }
    }
  ],
  "applicant": {
    "signatures": [
      "John Smith",
      "Jane Doe"
    ]
  }
}
Switch Account
Copy Location
Switch the active account for how you request data.
Only available for financial advisors and multi-account structures.
POST /iserver/account
Request Object:
Body Parameters
acctId:
String
.
Required
Identifier for the unique account to retrieve information from.
Value Format: “DU1234567”
Python
cURL
request_url = f"{baseUrl}/iserver/account"
json_content = {
  "acctId": "U1234567,
}
requests.post(url=request_url, json=json_content)
curl \
--url {{baseUrl}}/iserver/account \
--request POST \
--header 'Content-Type:application/json' \
--data '{
  "acctId": "U1234567,
}'
Response Object:
set:
bool.
Confirms that the account change was set.
acctId:
String.
Confirms the account switched to.
{
    "set": true,
    "acctId": "U1234567
}
Receive Brokerage Accounts
Copy Location
Returns a list of accounts the user has trading access to, their respective aliases and the currently selected account. Note this endpoint must be called before modifying an order or querying open orders.
GET /iserver/accounts
Request Object:
No parameters necessary.
Python
cURL
request_url = f"{baseUrl}/iserver/accounts" 
requests.get(url=request_url)
curl \
--url {{baseUrl}}/iserver/accounts \ 
--request GET
Response Object:
accounts:
Array of Strings.
Returns an array of all accessible accountIds.
acctProps:
Json Object.
Returns an json object for each accessible account’s properties.
hasChildAccounts:
bool.
Returns whether or not child accounts exist for the account.
supportsCashQty:
bool
Returns whether or not the account can use Cash Quantity for trading.
supportsFractions:
bool.
Returns whether or not the account can submit fractional share orders.
allowCustomerTime:
bool.
Returns whether or not the account must submit “manualOrderTime” with orders or not.
If true, manualOrderTime
must
be included.
If false, manualOrderTime
cannot
be included.
aliases:
JSON Object.
Returns any available aliases for the account.
allowFeatures:
JSON object
JSON of allowed features for the account.
showGFIS:
bool.
Returns if the account can access market data.
showEUCostReport:
bool.
Returns if the account can view the EU Cost Report
allowFXConv:
bool.
Returns if the account can convert currencies.
allowFinancialLens:
bool.
Returns if the account can access the financial lens.
allowMTA:
bool.
Returns if the account can use mobile trading alerts.
allowTypeAhead:
bool.
Returns if the account can use Type-Ahead support for Client Portal.
allowEventTrading:
bool.
Returns if the account can use Event Trader.
snapshotRefreshTimeout:
int.
Returns the snapshot refresh timeout window for new data.
liteUser:
bool.
Returns if the account is an IBKR Lite user.
showWebNews:
bool.
Returns if the account can use News feeds via the web.
research: bool.
debugPnl:
bool.
Returns if the account can use the debugPnl endpoint.
showTaxOpt:
bool.
Returns if the account can use the Tax Optimizer tool
showImpactDashboard:
bool.
Returns if the account can view the Impact Dashboard.
allowDynAccount:
bool.
Returns if the account can use dynamic account changes.
allowCrypto:
bool.
Returns if the account can trade crypto currencies.
allowedAssetTypes:
bool.
Returns a list of asset types the account can trade.
chartPeriods:
Json Object.
Returns available trading times for all available security types.
groups:
Array.
Returns an array of affiliated groups.
profiles:
Array.
Returns an array of affiliated profiles.
selectedAccount:
String.
Returns currently selected account. See
Switch Account
for more details.
serverInfo:
JSON Object.
Returns information about the IBKR session. Unrelated to Client Portal Gateway.
sessionId:
String.
Returns current session ID.
isFT:
bool.
Returns fractional trading access.
isPaper:
bool.
Returns account type status.
{
  "accounts": [
    "U1234567"
  ],
  "acctProps": {
    "U1234567": {
      "hasChildAccounts": false,
      "supportsCashQty": true,
      "noFXConv": false,
      "isProp": false,
      "supportsFractions": true,
      "allowCustomerTime": false
    }
  },
  "aliases": {
    "U1234567": "U1234567"
  },
  "allowFeatures": {
    "showGFIS": true,
    "showEUCostReport": false,
    "allowEventContract": true,
    "allowFXConv": true,
    "allowFinancialLens": false,
    "allowMTA": true,
    "allowTypeAhead": true,
    "allowEventTrading": true,
    "snapshotRefreshTimeout": 30,
    "liteUser": false,
    "showWebNews": true,
    "research": true,
    "debugPnl": true,
    "showTaxOpt": true,
    "showImpactDashboard": true,
    "allowDynAccount": false,
    "allowCrypto": false,
    "allowedAssetTypes": "STK,CRYPTO"
  },
  "chartPeriods": {
    "STK": [
      "*"
    ],
    "CRYPTO": [
      "*"
    ]
  },
  "groups": [],
  "profiles": [],
  "selectedAccount": "U1234567",
  "serverInfo": {
    "serverName": "JifN17091",
    "serverVersion": "Build 10.25.0p, Dec 5, 2023 5:48:12 PM"
  },
  "sessionId": "1234a5b.12345678",
  "isFT": false,
  "isPaper": false
}
Contract
Copy Location
Search the security definition by Contract ID
Copy Location
Returns a list of security definitions for the given conids
GET /trsrv/secdef
Request Object
Query Prams
conids:
int*.
Required
A comma separated series of contract IDs.
Value Format: 1234
Python
cURL
request_url = f"{baseUrl}/trsrv/secdef?conids=265598"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/trsrv/secdef?conids=265598 \
--request GET
Response Object
secdef
: array.
Returns the contents of the request with the array.
conid:
int.
Returns the conID
currency:
String.
Returns the traded currency for the contract.
time:
int.
Returns amount of time in ms to generate the data.
chineseName:
String.
Returns the Chinese characters for the symbol.
allExchanges:
String*.
Returns a series of exchanges the given symbol can trade on.
listingExchange:
String.
Returns the primary or listing exchange the contract is hosted on.
countryCode:
String.
Returns the country code the contract is traded on.
name:
String.
Returns the comapny name.
assetClass:
String.
Returns the asset class or security type of the contract.
expiry:
String.
Returns the expiry of the contract. Returns null for non-expiry instruments.
lastTradingDay:
String.
Returns the last trading day of the contract.
group:
String.
Returns the group or industry the contract is affilated with.
putOrCall:
String.
Returns if the contract is a Put or Call option.
sector:
String.
Returns the contract’s sector.
sectorGroup:
String.
Returns the sector’s group.
strike:
String.
Returns the strike of the contract.
ticker:
String.
Returns the ticker symbol of the traded contract.
undConid:
int.
Returns the contract’s underlyer.
multiplier:
float,
Returns the contract multiplier.
type:
String.
Returns stock type.
hasOptions:
bool.
Returns if contract has tradable options contracts.
fullName:
String.
Returns symbol name for requested contract.
isUS:
bool.
Returns if the contract is US based or not.
incrementRules & displayRule:
Array.
Returns rules regarding incrementation for order placement. Not functional for all exchanges. Please see
/iserver/contract/rules
for more accurate rule details.
isEventContract:
bool.
Returns if the contract is an event contract or not.
pageSize:
int.
Returns the content size of the request.
{
  "secdef": [
    {
      "conid": 265598,
      "currency": "USD",
      "time": 43,
      "chineseName": "苹果公司",
      "allExchanges": "AMEX,NYSE,CBOE,PHLX,CHX,ARCA,ISLAND,ISE,IDEAL,NASDAQQ,NASDAQ,DRCTEDGE,BEX,BATS,NITEECN,EDGEA,CSFBALGO,JEFFALGO,NYSENASD,PSX,BYX,ITG,PDQ,IBKRATS,CITADEL,NYSEDARK,MIAX,IBDARK,CITADELDP,NASDDARK,IEX,WEDBUSH,SUMMER,WINSLOW,FINRA,LIQITG,UBSDARK,BTIG,VIRTU,JEFF,OPCO,COWEN,DBK,JPMC,EDGX,JANE,NEEDHAM,FRACSHARE,RBCALGO,VIRTUDP,BAYCREST,FOXRIVER,MND,NITEEXST,PEARL,GSDARK,NITERTL,NYSENAT,IEXMID,HRT,FLOWTRADE,HRTDP,JANELP,PEAK6,IMCDP,CTDLZERO,HRTMID,JANEZERO,HRTEXST,IMCLP,LTSE,SOCGENDP,MEMX,INTELCROS,VIRTUBYIN,JUMPTRADE,NITEZERO,TPLUS1,XTXEXST,XTXDP,XTXMID,COWENLP,BARCDP,JUMPLP,OLDMCLP,RBCCMALP,WALLBETH,IBEOS,JONES,GSLP,BLUEOCEAN,USIBSILP,OVERNIGHT,JANEMID,IBATSEOS,HRTZERO,VIRTUALGO",
      "listingExchange": "NASDAQ",
      "countryCode": "US",
      "name": "APPLE INC",
      "assetClass": "STK",
      "expiry": null,
      "lastTradingDay": null,
      "group": "Computers",
      "putOrCall": null,
      "sector": "Technology",
      "sectorGroup": "Computers",
      "strike": "0",
      "ticker": "AAPL",
      "undConid": 0,
      "multiplier": 0.0,
      "type": "COMMON",
      "hasOptions": true,
      "fullName": "AAPL",
      "isUS": true,
      "incrementRules": [
        {
          "lowerEdge": 0.0,
          "increment": 0.01
        }
      ],
      "displayRule": {
        "magnification": 0,
        "displayRuleStep": [
          {
            "decimalDigits": 2,
            "lowerEdge": 0.0,
            "wholeDigits": 4
          }
        ]
      },
      "isEventContract": false,
      "pageSize": 100
    }
  ]
}
All Conids by Exchange
Copy Location
Send out a request to retrieve all contracts made available on a requested exchange. This returns all contracts that are tradable on the exchange, even those that are not using the exchange as their primary listing.
Note:
This is only available for Stock contracts.
GET /trsrv/all-conids
Request Object
Query Params
exchange:
String.
Required
Specify a single exchange to receive conids for.
Python
cURL
request_url = f"{baseUrl}/trsrv/all-conids?exchange=AMEX"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/trsrv/all-conids?exchange=AMEX \
--request GET
Response Object
ticker:
String.
Returns the ticker symbol of the contract
conid:
int.
Returns the contract identifier of the returned contract.
exchange:
String.
Returns the exchanger of the returned contract.
[
  {
    "ticker": "BMO",
    "conid": 5094,
    "exchange": "NYSE"
  },
  {...},
  {
    "ticker": "ZKH",
    "conid": 671347171,
    "exchange": "NYSE"
  }
]
Contract information by Contract ID
Copy Location
Requests full contract details for the given conid
GET /iserver/contract/{conid}/info
Request Object
Path Params:
conid:
String.
Contract ID for the desired contract information.
Python
cURL
request_url = f"{baseUrl}/iserver/contract/265598/info"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/iserver/contract/265598/info \ 
--request GET
Response Object
conid:
int.
Contract ID of the requested contract.
ticker:
String.
Ticker symbol of the requested contract.
secType:
String.
Security type of the requested contract.
listingExchange:
String.
Primary exchange of the requested contract.
exchange:
String.
Traded exchange of the requested contract set in the request.
companyName:
String.
Company name of the requested contract.
currency:
String.
National currency of the requested contract.
validExchanges:
String.
All valid exchanges of the requested contract.
priceRendering:
String.
Render price of the requested contract.
maturityDate:
String.
Maturity, or expiration date, of the requested contract.
right:
String.
Right, put or call, of the requested contract.
strike:
int.
Strike price of the requested contract.
{
  "cfi_code": "",
  "symbol": "AAPL",
  "cusip": null,
  "expiry_full": null,
  "con_id": 265598,
  "maturity_date": null,
  "industry": "Computers",
  "instrument_type": "STK",
  "trading_class": "NMS",
  "valid_exchanges": "SMART,AMEX,NYSE,CBOE,PHLX,ISE,CHX,ARCA,ISLAND,DRCTEDGE,BEX,BATS,EDGEA,JEFFALGO,BYX,IEX,EDGX,FOXRIVER,PEARL,NYSENAT,LTSE,MEMX,TPLUS1,IBEOS,OVERNIGHT,PSX",
  "allow_sell_long": false,
  "is_zero_commission_security": false,
  "local_symbol": "AAPL",
  "contract_clarification_type": null,
  "classifier": null,
  "currency": "USD",
  "text": null,
  "underlying_con_id": 0,
  "r_t_h": true,
  "multiplier": null,
  "underlying_issuer": null,
  "contract_month": null,
  "company_name": "APPLE INC",
  "smart_available": true,
  "exchange": "SMART",
  "category": "Computers"
}
Currency Pairs
Copy Location
Obtains available currency pairs corresponding to the given target currency.
GET /iserver/currency/pairs
Request Object
Query Params
currency:
String.
Required
Specify the target currency you would like to receive official pairs of.
Valid Structure: “USD”
Python
cURL
request_url = f"{baseUrl}/iserver/currency/pairs?currency=USD"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/iserver/currency/pairs?currency=USD \
--request GET
Response Object
{{currency}}:
List of Objects.
[{
symbol:
String.
The official symbol of the given currency pair.
conid:
int.
The official contract identifier of the given currency pair.
ccyPair:
String.
Returns the counterpart of
}]
{
  "USD": [
    {
      "symbol": "USD.SGD",
      "conid": 37928772,
      "ccyPair": "SGD"
    },
	{...},
    {
      "symbol": "USD.RUB",
      "conid": 28454968,
      "ccyPair": "RUB"
    }
  ]
}
Currency Exchange Rate
Copy Location
Obtains the exchange rates of the currency pair.
GET /iserver/exchangerate
Request Object
Query Params
Source:
String.
Required
Specify the base currency to request data for.
Valid Structure: “AUD”
Target:
String.
Required
Specify the quote currency to request data for.
Valid Structure: “USD”
Python
cURL
request_url = f"{baseUrl}/iserver/exchangerate?target=AUD&source=USD"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/iserver/exchangerate?target=AUD&source=USD \
--request GET
Response Object
rate:
float.
Returns the exchange rate for the currency pair.
{
    "rate": 0.67005002
}
Find all Info and Rules for a given contract
Copy Location
Returns both contract info and rules from a single endpoint.
For only contract rules, use the endpoint /iserver/contract/rules.
For only contract info, use the endpoint /iserver/contract/{conid}/info.
GET /iserver/contract/{{ conid }}/info-and-rules
Request Object
Path Parameters
coind:
String.
Required
Contract identifier for the given contract.
Query Parameters
isBuy:
bool.
Indicates whether you are searching for Buy or Sell order rules.
Set to true for Buy Orders, set to false for Sell Orders
Python
cURL
request_url = f"{baseUrl}/iserver/contract/265598/info-and-rules?isBuy=true"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/iserver/contract/265598/info-and-rules?isBuy=true \ 
--request GET
Response Object
cfi_code:
String.
Classification of Financial Instrument codes
symbol:
String.
Underlying symbol
cusip:
String.
Returns the CUSIP for the given instrument.
Only used in BOND trading.
expiry_full:
String.
Returns the expiration month of the contract.
Formatted as “YYYYMM”
con_id:
int.
Indicates the contract identifier of the given contract.
maturity_date:
String.
Indicates the final maturity date of the given contract.
Formatted as “YYYYMMDD”
industry:
String.
Specific group of companies or businesses.
instrument_type:
String.
Asset class of the instrument.
trading_class:
String.
Designated trading class of the contract.
valid_exchanges:
String.
Comma separated list of support exchanges or trading venues.
allow_sell_long:
bool.
Allowed to sell shares you own.
is_zero_commission_security:
bool.
Indicates if the contract supports zero commission trading.
local_symbol:
String.
Contract’s symbol from primary exchange. For options it is the OCC symbol.
contract_clarification_type:
null
classifier:
null.
currency:
String.
Base currency contract is traded in.
text:
String.
Indicates the display name of the contract, as shown with Client Portal.
underlying_con_id:
int.
Underlying contract identifier for the requested contract.
r_t_h:
bool.
Indicates if the contract can be traded outside regular trading hours or not.
multiplier:
String.
Indicates the multiplier of the contract.
underlying_issuer:
String.
Indicates the issuer of the underlying.
contract_month:
String.
Indicates the year and month the contract expires.
Value Format: “YYYYMM”
company_name:
String.
Indicates the name of the company or index.
smart_available:
bool.
Indicates if the contract can be smart routed or not.
exchange:
String.
Indicates the primary exchange for which the contract can be traded.
category:
String.
Indicates the industry category of the instrument.
rules:
Object.
See the
/iserver/contract/rules
endpoint.
{
  "cfi_code": "",
  "symbol": "AAPL",
  "cusip": null,
  "expiry_full": null,
  "con_id": 265598,
  "maturity_date": null,
  "industry": "Computers",
  "instrument_type": "STK",
  "trading_class": "NMS",
  "valid_exchanges": "SMART,AMEX,NYSE,CBOE,PHLX,ISE,CHX,ARCA,ISLAND,DRCTEDGE,BEX,BATS,EDGEA,JEFFALGO,BYX,IEX,EDGX,FOXRIVER,PEARL,NYSENAT,LTSE,MEMX,TPLUS1,IBEOS,OVERNIGHT,PSX",
  "allow_sell_long": false,
  "is_zero_commission_security": false,
  "local_symbol": "AAPL",
  "contract_clarification_type": null,
  "classifier": null,
  "currency": "USD",
  "text": null,
  "underlying_con_id": 0,
  "r_t_h": true,
  "multiplier": null,
  "underlying_issuer": null,
  "contract_month": null,
  "company_name": "APPLE INC",
  "smart_available": true,
  "exchange": "SMART",
  "category": "Computers",
  "rules": {
    "algoEligible": true,
    "overnightEligible": true,
    "costReport": false,
    "canTradeAcctIds": [
      "U1234567"
    ],
    "error": null,
    "orderTypes": [
      "limit",
      "midprice",
      "market",
      "stop",
      "stop_limit",
      "mit",
      "lit",
      "trailing_stop",
      "trailing_stop_limit",
      "relative",
      "marketonclose",
      "limitonclose"
    ],
    "ibAlgoTypes": [
      "limit",
      "stop_limit",
      "lit",
      "trailing_stop_limit",
      "relative",
      "marketonclose",
      "limitonclose"
    ],
    "fraqTypes": [
      "limit",
      "market",
      "stop",
      "stop_limit",
      "mit",
      "lit",
      "trailing_stop",
      "trailing_stop_limit"
    ],
    "forceOrderPreview": false,
    "cqtTypes": [
      "limit",
      "market",
      "stop",
      "stop_limit",
      "mit",
      "lit",
      "trailing_stop",
      "trailing_stop_limit"
    ],
    "orderDefaults": {
      "LMT": {
        "LP": "197.93"
      }
    },
    "orderTypesOutside": [
      "limit",
      "stop_limit",
      "lit",
      "trailing_stop_limit",
      "relative"
    ],
    "defaultSize": 100,
    "cashSize": 0.0,
    "sizeIncrement": 100,
    "tifTypes": [
      "IOC/MARKET,LIMIT,RELATIVE,MARKETONCLOSE,MIDPRICE,LIMITONCLOSE,MKT_PROTECT,STPPRT,a",
      "GTC/o,a",
      "OPG/LIMIT,MARKET,a",
      "GTD/o,a",
      "DAY/o,a"
    ],
    "tifDefaults": {
      "TIF": "DAY",
      "SIZE": "100.00"
    },
    "limitPrice": 197.93,
    "stopprice": 197.93,
    "orderOrigination": null,
    "preview": true,
    "displaySize": null,
    "fraqInt": 4,
    "cashCcy": "USD",
    "cashQtyIncr": 500,
    "priceMagnifier": null,
    "negativeCapable": false,
    "incrementType": 1,
    "incrementRules": [
      {
        "lowerEdge": 0.0,
        "increment": 0.01
      }
    ],
    "hasSecondary": true,
    "increment": 0.01,
    "incrementDigits": 2
  }
}
Search Algo Params by Contract ID
Copy Location
Returns supported IB Algos for contract.
A pre-flight request must be submitted before retrieving information
GET /iserver/contract/{{ conid }}/algos
Request Object
Path Parameters
conid:
String. Required
Contract identifier for the requested contract of interest.
Query Parameters
algos:
String. Optional
List of algo ids delimited by “;” to filter by.
Max of 8 algos ids can be specified.
Case sensitive to algo id.
addDescription:
String. Optional
Whether or not to add algo descriptions to response. Set to 1 for yes, 0 for no.
addParams:
String. Optional
Whether or not to show algo parameters. Set to 1 for yes, 0 for no.
Python
cURL
request_url = f"{baseUrl}/iserver/contract/265598/algos?algos=Adaptive;Vwap&addDescription=1&addParams=1"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/iserver/contract/265598/algos?algos=Adaptive;Vwap&addDescription=1&addParams=1 \
--request GET
Response Object
algos:
Array of objects.
Contains all relevant algos for the contract.
[{
name:
String.
Common name of the algo.
id:
String.
Algo identifier used for requests
parameters:
Array of objects.
All parameters relevant to the given algo.
Only returned if addParams=1
[{
guiRank:
int.
Positional ranking for the algo. Used for Client Portal.
defaultValue:
int.
Default parameter value.
name:
String.
Parameter name.
id:
String.
Parameter identifier for the algo.
legalStrings:
Array
Allowed values for the parameter.
required:
String.
States whether the parameter is required for the given algo order to place.
Returns a string representation of a boolean.
valueClassName:
String.
Returns the variable type of the parameter.
}]
}]
{
  "algos": [
    {
      "name": "Adaptive",
      "id": "Adaptive",
      "parameters": [
        {
          "guiRank": 1,
          "defaultValue": "Normal",
          "name": "Adaptive order priority/urgency",
          "id": "adaptivePriority",
          "legalStrings": [
            "Urgent",
            "Normal",
            "Patient"
          ],
          "required": "true",
          "valueClassName": "String"
        }
      ]
    },
    {
      "name": "VWAP",
      "id": "Vwap",
      "parameters": [
        {
          "guiRank": 5,
          "defaultValue": false,
          "name": "Attempt to never take liquidity",
          "id": "noTakeLiq",
          "valueClassName": "Boolean"
        },
        {
          "guiRank": 11,
          "defaultValue": false,
          "name": "Opt-out closing auction",
          "id": "optoutClosingAuction",
          "valueClassName": "Boolean"
        },
        {
          "guiRank": 4,
          "defaultValue": false,
          "name": "Allow trading past end time",
          "id": "allowPastEndTime",
          "valueClassName": "Boolean"
        },
        {
          "guiRank": 8,
          "defaultValue": false,
          "name": "Speed up when market approaches limit price",
          "description": "Compensate for decreased fill rate due to presence of limit price.",
          "id": "speedUp",
          "enabledConditions": [
            "MKT:speedUp:=:no"
          ],
          "valueClassName": "Boolean"
        },
        {
          "guiRank": 12,
          "name": "Trade when price is more aggressive than:",
          "description": "Evaluates with bid for buy order and ask for sell order",
          "id": "conditionalPrice",
          "valueClassName": "Double"
        },
        {
          "guiRank": 2,
          "name": "Start Time",
          "description": "Defaults to start of market trading",
          "id": "startTime",
          "valueClassName": "Time"
        },
        {
          "guiRank": 1,
          "minValue": 0.01,
          "maxValue": 50,
          "name": "Max Percentage",
          "description": "From 0.01 to 50.0",
          "id": "maxPctVol",
          "valueClassName": "Double"
        },
        {
          "guiRank": 3,
          "name": "End Time",
          "description": "Defaults to end of market trading",
          "id": "endTime",
          "valueClassName": "Time"
        }
      ]
    }
  ]
}
Search Bond Filter Information
Copy Location
Request a list of filters relating to a given Bond issuerID. The issuerId is retrieved from
/iserver/secdef/search
and can be used in
/iserver/secdef/info?issuerId={{ issuerId }}
for retrieving conIds.
/iserver/secdef/bond-filters
Request Object
Query Params
symbol:
String.
Required
This should always be set to “BOND”
issuerId:
String.
Required
Specifies the issuerId value used to designate the bond issuer type.
Python
cURL
request_url = f"{baseUrl}/iserver/secdef/bond-filters?symbol=BOND&issuerId=e1400715"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/iserver/secdef/bond-filters?symbol=BOND&issuerId=e1400715 \
--request GET
bondFilters:
Array of Objects.
Contains all filters pertaining to the given issuerId.
[{
displayText:
String.
An identifier used to document returned options/values. This can be thought of as a key value.
columnId:
int.
Used for user interfaces. Internal use only.
options:
Array of objects.
Contains all objects with values corresponding to the parent displayText key.
[{
text:
String.
In some instances, a text value will be returned, which indicates the standardized value format such as plaintext dates, rather than solely numerical values.
value:
String.
Returns value directly correlating to the displayText key. This may include exchange, maturity date, issue date, coupon, or currency.
}]
}]
{
  "bondFilters": [
    {
      "displayText": "Exchange",
      "columnId": 0,
      "options": [
      {
        "value": "SMART"
      }]
    },
    {
      "displayText": "Maturity Date",
      "columnId": 27,
      "options": [
        {
          "text": "Jan 2025",
          "value": "202501"
      }]
    },
    {
      "displayText": "Issue Date",
      "columnId": 28,
      "options": [{
        "text": "Sep 18 2014",
        "value": "20140918"
      }]
    },
    {
      "displayText": "Coupon",
      "columnId": 25,
      "options": [{
        "value": "1.301"
      }]
    },
    {
      "displayText": "Currency",
      "columnId": 5,
      "options": [{
        "value": "EUR"
      }]
    }
  ]
}
Search Contract by Symbol
Copy Location
Search by underlying symbol or company name. Relays back what derivative contract(s) it has. This endpoint must be called before using /secdef/info.
For bonds, enter the family type in the symbol field to receive the issuerID used in the /iserver/secdef/info endpoint.
GET /iserver/secdef/search
Request Object
Query Params
symbol:
String.
Required
Underlying symbol of interest. May also pass company name if ‘name’ is set to true, or bond issuer type to retrieve bonds.
name:
bool.
Determines if symbol reflects company name or ticker symbol. If company name is included will only receive limited response: conid, companyName, companyHeader and symbol. The inclusion of the name field will prohibit the
/iserver/secdef/strikes
endpoint from returning data. After retrieving your expected contract, customers looking to create option chains should remove the name field from the request.
secType:
String.
Valid Values: “STK”, “IND”, “BOND”
Declares underlying security type.
Python
cURL
request_url = f"{baseUrl}/iserver/secdef/search?symbol=Interactive Brokers&name=true"
requests.get(url=request_url)
curl --insecure \
--url https://localhost:5000/v1/api/iserver/secdef/search?symbol=Interactive Brokers&name=true \
--request GET
Response Object
“conid”:
String.
Conid of the given contract.
“companyHeader”:
String.
Extended company name and primary exchange.
“companyName”:
String.
Name of the company.
“symbol”:
String.
Company ticker symbol.
“description”:
String.
Primary exchange of the contract.
“restricted”:
bool.
Returns if the contract is available for trading.
“sections”:
Array of objects
“secType”:
String.
Given contracts security type.
“months”:
String.
Returns a string of dates, separated by semicolons.
Value Format: “JANYY;FEBYY;MARYY”
“symbol”:
String.
Symbol of the instrument.
“exchange”:
String.
Returns a string of exchanges, separated by semicolons.
Value Format: “EXCH;EXCH;EXCH”
Unique for Bonds
“issuers”:
Array of objects
Array of objects containing the id and name for each bond issuer.
“id”:
String.
Issuer Id for the given contract.
“name”:
String.
Name of the issuer.
“bondid”:
int.
Bond type identifier.
“conid”:
String.
Contract ID for the given bond.
“companyHeader”:
String.
Name of the bond type
Value Format: “Corporate Fixed Income”
“companyName”:
null
Returns ‘null’ for bond contracts.
“symbol”:null
Returns ‘null’ for bond contracts.
“description”:null
Returns ‘null’ for bond contracts.
“restricted”:null
Returns ‘null’ for bond contracts.
“fop”:null
Returns ‘null’ for bond contracts.
“opt”:null
Returns ‘null’ for bond contracts.
“war”:null
Returns ‘null’ for bond contracts.
“sections”:
Array of objects
Only relays “secType”:”BOND” in the Bonds section.
[
  {
    "conid": "43645865",
    "companyHeader": "IBKR INTERACTIVE BROKERS GRO-CL A (NASDAQ) ",
    "companyName": "INTERACTIVE BROKERS GRO-CL A (NASDAQ)",
    "symbol": "IBKR",
    "description": null,
    "restricted": null,
    "sections": [],
    "secType": "STK"
  }
]
Search Contract Rules
Copy Location
Returns trading related rules for a specific contract and side.
POST /iserver/contract/rules
Request Object
Body Parameters
conid:
Number. Required
Contract identifier for the interested contract.
exchange:
String.
Designate the exchange you wish to receive information for in relation to the contract.
isBuy:
bool.
Side of the market rules apply too. Set to true for Buy Orders, set to false for Sell Orders
Defaults to true or Buy side rules.
modifyOrder:
bool.
Used to find trading rules related to an existing order.
orderId:
Number.
Required for modifyOrder:true
Specify the order identifier used for tracking a given order.
Python
cURL
request_url = f"{baseUrl}/iserver/contract/rules"
json_content = {
  "conid": 265598,
  "exchange": "SMART",
  "isBuy": true,
  "modifyOrder": true,
  "orderId": 1234567890
}
requests.post(url=request_url, json=json_content)
curl \
--url {{baseUrl}}/iserver/contract/rules \
--request POST \
--header 'Content-Type:application/json' \
--data '{
  "conid": 265598,
  "exchange": "SMART",
  "isBuy": true,
  "modifyOrder": true,
  "orderId": 1234567890
}'
Response Object
algoEligible:
bool.
Indicates if the contract can trade algos or not.
overnightEligible:
bool.
Indicates if outsideRTH trading is permitted for the instrument
costReport:
bool.
Indicates whether or not a cost report has been requested (Client Portal only).
canTradeAcctIds:
Array of Strings.
Indicates permitted accountIDs that may trade the contract.
error:
String.
If rules information can not be received for any reason, it will be expressed here.
orderTypes:
Array of Strings
Indicates permitted order types for use with standard quantity trading.
ibAlgoTypes:
Array of Strings.
Indicates permitted algo types for use with the given contract.
fraqTypes:
Array of Strings.
Indicates permitted order types for use with fractional trading.
forceOrderPreview:
bool.
Indicates if the order preview is forced upon the user before submission.
cqtTypes:
Array of Strings.
Indicates accepted order types for use with cash quantity.
orderDefaults:
Object of objects
Indicates default order type for the given security type.
orderTypesOutside:
Array of Strings.
Indicates permitted order types for use outside of regular trading hours.
defaultSize:
int.
Default total quantity value for orders.
cashSize:
float.
Default cash value quantity.
sizeIncrement:
int.
Indicates quantity increase for the contract.
tifTypes:
Array of Strings.
Indicates allowed tif types supported for the contract.
tifDefaults:
Object.
Object containing details about your TIF value defaults.
These defaults can be viewed and modified in TWS’s within the Global Configuration.
limitPrice:
float.
Default limit price for the given contract.
stopprice:
float.
Default stop price for the given contract.
orderOrigination:
String.
Order origin designation for US securities options and Options Clearing Corporation
preview:
bool.
Indicates if the order preview is required (for client portal only)
displaySize:
int.
fraqInt:
int.
Indicates decimal places for fractional order size
cashCcy:
String.
Indicates base currency for the instrument.
cashQtyIncr:
int.
Indicates cash quantity increment rules.
priceMagnifier:
int.
Signifies if a contract is not trading in the standard cash denomination.
If a symbol is priced in Cents, Pence, or the currency’s fractional equivalent, the relative value will be displayed. For standard instruments, Null will be passed.
negativeCapable:
bool.
Indicates if the value of the contract can be negative (true) or if it is always positive (false).
incrementType:
int.
Indicates the type of increment style.
incrementRules:
Array of objects.
Indicates increment rule values including lowerEdge and increment value.
hasSecondary:
bool.
modTypes:
Array of Strings.
Lists the available order types supported when modifying the order.
increment:
float.
Minimum increment values for prices
incrementDigits:
int.
Number of decimal places to indicate the increment value.
{
  "algoEligible": true,
  "overnightEligible": true,
  "costReport": false,
  "canTradeAcctIds": [
    "U1234567"
  ],
  "error": null,
  "orderTypes": [
    "limit",
    "midprice",
    "market",
    "stop",
    "stop_limit",
    "mit",
    "lit",
    "trailing_stop",
    "trailing_stop_limit",
    "relative",
    "marketonclose",
    "limitonclose"
  ],
  "ibAlgoTypes": [
    "limit",
    "stop_limit",
    "lit",
    "trailing_stop_limit",
    "relative",
    "marketonclose",
    "limitonclose"
  ],
  "fraqTypes": [],
  "forceOrderPreview": false,
  "cqtTypes": [
    "limit",
    "market",
    "stop",
    "stop_limit",
    "mit",
    "lit",
    "trailing_stop",
    "trailing_stop_limit"
  ],
  "orderDefaults": {
    "LMT": {
      "LP": "549000.00"
    }
  },
  "orderTypesOutside": [
    "limit",
    "stop_limit",
    "lit",
    "trailing_stop_limit",
    "relative"
  ],
  "defaultSize": 100,
  "cashSize": 0.0,
  "sizeIncrement": 1,
  "tifTypes": [
    "IOC/MARKET,LIMIT,RELATIVE,MARKETONCLOSE,MIDPRICE,LIMITONCLOSE,MKT_PROTECT,STPPRT,a",
    "GTC/o,a",
    "OPG/LIMIT,MARKET,a",
    "GTD/o,a",
    "DAY/o,a"
  ],
  "tifDefaults": {
    "TIF": "DAY",
    "SIZE": "100.00"
  },
  "limitPrice": 549000.0,
  "stopprice": 549000.0,
  "orderOrigination": null,
  "preview": true,
  "displaySize": null,
  "fraqInt": 0,
  "cashCcy": "USD",
  "cashQtyIncr": 500,
  "priceMagnifier": null,
  "negativeCapable": false,
  "incrementType": 1,
  "incrementRules": [
    {
      "lowerEdge": 0.0,
      "increment": 0.01
    }
  ],
  "hasSecondary": true,
  "increment": 0.01,
  "incrementDigits": 2
}
Search SecDef information by conid
Copy Location
Provides Contract Details of Futures, Options, Warrants, Cash and CFDs based on conid.
For all instruments,
/iserver/secdef/search
must be called first.
For derivatives such as Options, Warrants, and Futures Options, you will need to query
/iserver/secdef/strikes
as well.
GET /iserver/secdef/info
Request Object
Query Parameters
conid:
String.
Required
Contract identifier of the underlying. May also pass the final derivative conid directly.
sectype:
String.
Required
Security type of the requested contract of interest.
month:
String.
Required for Derivatives
Expiration month for the given derivative.
exchange:
String.
Optional
Designate the exchange you wish to receive information for in relation to the contract.
strike:
String.
Required for Options and Futures Options
Set the strike price for the requested contract details
right:
String.
Required for Options
Set the right for the given contract.
Value Format: “C” for Call or “P” for Put.
issuerId:
String.
Required for Bonds
Set the issuerId for the given bond issuer type.
Example Format: “e1234567”
Python
cURL
request_url = f"{baseUrl}/iserver/secdef/info?conid=265598&secType=OPT&month=JAN24&strike=195&right=P"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/iserver/secdef/info?conid=265598&secType=OPT&month=JAN24&strike=195&right=P \
--request GET
Response Object
conid:
int.
Contract Identifier of the given contract
ticker:
String
Ticker symbol for the given contract
secType:
String.
Security type for the given contract.
listingExchange:
String.
Primary listing exchange for the given contract.
exchange:
String.
Exchange requesting data for.
companyName:
String.
Name of the company for the given contract.
currency:
String
Traded currency allowed for the given contract.
validExchanges:
String*
Series of all valid exchanges the contract can be traded on in a single comma-separated string.
priceRendering: null.
maturityDate:
String
Date of expiration for the given contract.
right:
String.
Right (P or C) for the given contract.
strike:
Float.
Returns the given strike value for the given contract.
[
  {
    "conid": 667629330,
    "symbol": "AAPL",
    "secType": "OPT",
    "exchange": "SMART",
    "listingExchange": null,
    "right": "P",
    "strike": 195.0,
    "currency": "USD",
    "cusip": null,
    "coupon": "No Coupon",
    "desc1": "AAPL",
    "desc2": "JAN 05 '24 195 Put",
    "maturityDate": "20240105",
    "multiplier": "100",
    "tradingClass": "AAPL",
    "validExchanges": "SMART,AMEX,CBOE,PHLX,PSE,ISE,BOX,BATS,NASDAQOM,CBOE2,NASDAQBX,MIAX,GEMINI,EDGX,MERCURY,PEARL,EMERALD,MEMX,IBUSOPT"
  }
]
Search Strikes by Underlying Contract ID
Copy Location
Query to receive a list of potential strikes supported for a given underlying.
This endpoint will always return empty arrays unless
/iserver/secdef/search
is called for the same underlying symbol beforehand. The inclusion of the name field with the
/iserver/secdef/search
endpoint will prohibit the strikes endpoint from returning data. After retrieving your expected contract from the initial search, developers looking to create option chains should remove the name field from the request.
GET /iserver/secdef/strikes
Request Object
Query Parameters
conid:
String.
Required
Contract Identifier number for the underlying
sectype:
String.
Required
Security type of the derivatives you are looking for.
Value Format: “OPT” or “WAR”
month:
String.
Required
Expiration month and year for the given underlying
Value Format: {3 character month}{2 character year}
Example: AUG23
exchange:
String.
Optional
Exchange from which derivatives should be retrieved from.
Default value is set to SMART
Python
cURL
request_url = f"{baseUrl}/iserver/secdef/strikes?conid=265598&sectype=OPT&month=JAN24&exchange=SMART"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/iserver/secdef/strikes?conid=265598&sectype=OPT&month=JAN24&exchange=SMART \
--request GET
Response Object
call:
Array of Floats
Array containing a series of comma separated float values representing potential call strikes for the instrument.
put:
Array of Floats
Array containing a series of comma separated float values representing potential put strikes for the instrument.
{
  "call":[
    185.0,
    190.0,
    195.0,
    200.0
  ],
  "put":[
    185.0,
    190.0,
    195.0,
    200.0
  ]
}
Security Future by Symbol
Copy Location
Returns a list of non-expired future contracts for given symbol(s)
GET /trsrv/futures
Request Object
Query Params
symbols
:
String
.
Required
Indicate the symbol(s) of the underlier you are trying to retrieve futures on. Accepts comma delimited string of symbols.
Python
cURL
request_url = f"{baseUrl}/trsrv/futures?symbols=ES,MES"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/trsrv/futures?symbols=ES,MES \
--request GET
Response Body
symbol:
Array
Displayed as the string of your symbol
Contains a series of objects for each symbol that matches the requested.
symbol:
String.
The requested symbol value.
conid:
int.
Contract identifier for the specific symbol
underlyingConid:
int.
Contract identifier for the future’s underlying contract.
expirationDate:
int.
Expiration date of the specific future contract.
ltd:
int.
Last trade date of the future contract.
shortFuturesCutOff:
int.
Represents the final day for contract rollover for shorted futures.
longFuturesCutOff:
int.
Represents the final day for contract rollover for long futures.
{
  "ES": [
    {
      "symbol": "ES",
      "conid": 495512552,
      "underlyingConid": 11004968,
      "expirationDate": 20231215,
      "ltd": 20231214,
      "shortFuturesCutOff": 20231214,
      "longFuturesCutOff": 20231214
    },
    {...}
  ],
  "MES": [
    {
      "symbol": "MES",
      "conid": 586139726,
      "underlyingConid": 362673777,
      "expirationDate": 20231215,
      "ltd": 20231215,
      "shortFuturesCutOff": 20231215,
      "longFuturesCutOff": 20231215
    },
    {...}
  ]
}
Security Stocks by Symbol
Copy Location
Returns an object contains all stock contracts for given symbol(s)
GET /trsrv/stocks
Request Object
Query Params
symbols
: String.
Comma-separated list of stock symbols. Symbols must contain only capitalized letters.
Python
cURL
request_url = f"{baseUrl}/trsrv/stocks?symbols=AAPL,IBKR"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/trsrv/stocks?symbols=AAPL,IBKR\
--request GET
Response Object
symbol:
Array of Json
Contains a series of Json for all contracts that match the symbol.
name:
String.
Full company name for the given contract.
chineseName:
String.
Chinese name for the given company.
assetClass:
String.
Asset class for the given company.
contracts:
Array.
A series of arrays pertaining to the same company listed by “name”.
Typically differentiated based on currency of the primary exchange.
conid:
int.
Contract ID for the specific contract.
exchange:
String.
Primary exchange for the given contract.
isUS:
bool.
States whether the contract is hosted in the United States or not.
{
  "AAPL": [
    {
      "name": "APPLE INC",
      "chineseName": "苹果公司",
      "assetClass": "STK",
      "contracts": [
        {
          "conid": 265598,
          "exchange": "NASDAQ",
          "isUS": true
        },
        {
          "conid": 38708077,
          "exchange": "MEXI",
          "isUS": false
        },
        {
          "conid": 273982664,
          "exchange": "EBS",
          "isUS": false
        }
      ]
    },
    {
      "name": "LS 1X AAPL",
      "chineseName": null,
      "assetClass": "STK",
      "contracts": [
        {
          "conid": 493546048,
          "exchange": "LSEETF",
          "isUS": false
        }
      ]
    },
    {
      "name": "APPLE INC-CDR",
      "chineseName": "苹果公司",
      "assetClass": "STK",
      "contracts": [
        {
          "conid": 532640894,
          "exchange": "AEQLIT",
          "isUS": false
        }
      ]
    }
  ]
}
Trading Schedule by Symbol
Copy Location
Returns the trading schedule up to a month for the requested contract
GET /trsrv/secdef/schedule
Request Object
Query Params
assetClass:
String.
Required
Specify the security type of the given contract.
Value Formats: Stock: STK, Option: OPT, Future: FUT, Contract For Difference: CFD, Warrant: WAR, Forex: SWP, Mutual Fund: FND, Bond: BND, Inter-Commodity Spreads: ICS
conid:
String.
Required
Provide the contract identifier to retrieve the trading schedule for.
symbol:
String.
Required
Specify the symbol for your contract.
exchange:
String.
Specify the primary exchange of your contract.
exchangeFilter:
String.
Specify exchange you want to retrieve data from.
Python
cURL
request_url = f"{baseUrl}/trsrv/secdef//schedule?assetClass=STK&conid=265598&symbol=AAPL&exchange=ISLAND&exchangeFilter=ISLAND"
requests.get(url=requests_url)
curl \
--url {{baseUrl}}/secdef/trsrv/schedule?assetClass=STK&symbol=AAPL&exchange=ISLAND&exchangeFilter=ISLAND,NYSE,AMEX \
--request GET
Response Object
id:
String.
Exchange parameter id
tradeVenueId:
String.
Reference on a trade venue of given exchange parameter
schedules:
Array of Objets.
Always contains at least one ‘tradingTime’ and zero or more ‘sessionTime’ tags
clearingCycleEndTime:
int.
End of clearing cycle.
tradingScheduleDate:
int.
Date of the clearing schedule.
20000101 stands for any Sat, 20000102 stands for any Sun, … 20000107 stands for any Fri. Any other date stands for itself.
sessions:
Object.
description: String.
If the LIQUID hours differs from the total trading day then a separate ‘session’ tag is returned.
openingTime:
int.
Opening date time of the session.
closingTime:
int.
Closing date time of the sesion.
prop:
String.
If the whole trading day is considered LIQUID then the value ‘LIQUID’ is returned.
tradingTimes:
Object.
Object containing trading times.
description:
String
Returns tradingTime in exchange time zone.
openingTime:
int.
Opening time of the trading day.
closingTime:
int.
Closing time of the trading day.
cancelDayOrders:
string.
Cancel time for day orders.
[
  {
    "id": "p102082",
    "tradeVenueId": "v13133",
    "timezone": "America/New_York",
    "schedules": [      
      {
        "clearingCycleEndTime": "2000",
        "tradingScheduleDate": "20000103",
        "sessions": [
          {
            "openingTime": "0930",
            "closingTime": "1600",
            "prop": "LIQUID"
          }
        ],
        "tradingtimes": [
          {
            "openingTime": "0400",
            "closingTime": "2000",
            "cancelDayOrders": "Y"
          }
        ]
      },
      {...}
    ]
  }
]
Trading Schedule (NEW)
Copy Location
Returns the trading schedule for the 6 total days surrounding the current trading day. Non-Trading days, such as holidays, will not be returned.
GET /contract/trading-schedule
Request Object
Query Params
conid:
String.
Required
Provide the contract identifier to retrieve the trading schedule for.
exchange:
String.
Accepts the exchange to retrieve data from. Primary exchange is assumed by default.
Python
cURL
request_url = f"{baseUrl}/contract/trading-schedule?conid=265598&exchange=ISLAND"
requests.get(url=requests_url)
curl \
--url {{baseUrl}}/contract/trading-schedule?conid=265598&exchange=ISLAND \
--request GET
Response Object
exchange_time_zone:
String.
Returns the time zone the exchange trades in.
schedules:
Object.
A schedule object containing the trading hours.
{
{date}:
Array.
Array of hours objects detailing extended and standard trading.
[
extended_hours:
Array.
Reference the total extended trading hours for the session.
{
cancel_daily_orders:
Boolean.
Determines if DAY orders are canceled after ‘closing’ time.
closing:
Integer.
Epoch timestamp of the exchange’s close.
opening:
Integer.
Epoch timestamp of the exchange’s open.
}
liquid_hours:
Array.
Reference the available trading hours for the regular session
{
closing:
Integer.
Epoch timestamp of the exchange’s close.
opening:
Integer.
Epoch timestamp of the exchange’s open.
}]}
{
  'exchange_time_zone': 'US/Central', 
  'schedules': {
    '20251218': {
      'extended_hours': [{
        'cancel_daily_orders': True,
        'closing': 1766095200,
        'opening': 1766012400}],
    'liquid_hours': [{
        'closing': 1766095200,
        'opening': 1766068200
    }]},
    '20251219': {
      'extended_hours': [{
        'cancel_daily_orders': True,
        'closing': 1766181600,
        'opening': 1766098800}],
    'liquid_hours': [{
        'closing': 1766181600,
        'opening': 1766154600
    }]},
    '20251222': {
      'extended_hours': [{
        'cancel_daily_orders': True,
        'closing': 1766440800,
        'opening': 1766358000}],
    'liquid_hours': [{
        'closing': 1766440800,
        'opening': 1766413800
    }]},
    '20251223': {
      'extended_hours': [{
        'cancel_daily_orders': True,
        'closing': 1766527200,
        'opening': 1766444400
		}],
    'liquid_hours': [{
        'closing': 1766527200,
        'opening': 1766500200
    }]},
    '20251224': {
      'extended_hours': [{
        'cancel_daily_orders': True,
        'closing': 1766600100,
        'opening': 1766530800}],
    'liquid_hours': [{
        'closing': 1766600100,
        'opening': 1766586600
    }]},
    '20251226': {
      'extended_hours': [{
        'cancel_daily_orders': True,
        'closing': 1766786400,
        'opening': 1766703600
    }]}	
  }
}
Event Contracts
Copy Location
Interactive Brokers models Event Contract instruments on options (for ForecastEx products) and futures options (for CME Group products).
Event Contracts can generally be thought of as options products in the Web API, and their discovery workflow follows a familiar options-like sequence. This guide will make analogies to conventional index options for both ForecastEx and CME Group products.
IB’s Event Contract instrument records use the following fields inherited from the options model:
An
underlier
, which may or may not be artificial:
For
CME products
, a tradable Event Contract will have the relevant CME future as its underlier.
For
ForecastEx products
, IB has generated an artificial underlying index which serves as a container for related Event Contracts in the same product class. These artificial indices do not have any associated reference values and are purely an artifact of the option instrument model used to represent these Event Contracts. However, these artificial underlying indices can be used to search for groups of related Event Contracts, just as with index options.
A
Symbol
value which matches the symbol of the underlier, and which reflects the issuer’s product code.
A
Trading Class
which also reflects the issuer’s product code for the instrument, and in the case of CME Group products, is used to differentiate Event Contracts from CME futures options.
Note that many CME Group Event Contracts, which resolve against CME Group futures, are assigned a Trading Class prefixed with “EC” and followed by the symbol of the relevant futures product, to avoid naming collisions with other derivatives (i.e., proper futures options listed on the same future).
A
Put or Call (Right)
value, where Call = Yes and Put = No.
Note that ForecastEx instruments do not permit Sell orders. Instead, ForecastEx positions are flattened or reduced by buying the opposing contract. CME Group Event Contracts permit both buying and selling.
An artificial
Contract Month
value, again used primarily for searching and filtering available instruments. Most Event Contract products do not follow monthly series as is common with index or equity options, so these Contract Month values are typically not a meaningful attribute of the instrument. Rather, they permit filtering of instruments by calendar month.
A
Last Trade Date, Time, and Millisecond
values, which together indicate precisely when trading in an Event Contract will cease, just as with index options.
An
Expected Resolution Time
when the outcome of the tracked event is published, and contracts are determined to be in or out of the money. This is commonly referred to as the contract’s “expiration date”.
An
Expected Payout Time
when contracts are settled and removed from accounts. Proceeds are paid out to those expiring in the money.
A
Measured Period
as defined in the contract’s question. This is the primary date used for organization of contracts (as in an options chain).
A
Strike
value, which is the numerical value on which the event resolution hinges. Though numerical, this value need not represent a price.
An
instrument description (or “local symbol”)
in the form
"PRODUCT EXPIRATION STRIKE RIGHT"
, where:
PRODUCT
is the issuer’s product identifier
EXPIRATION
is the date of the instrument’s resolution in the form
MmmDD'YY
, e.g., “Sep26’24”
STRIKE
is the numerical value that determines the contract’s moneyness at expiration
RIGHT
is a value YES or NO
Categorization
Copy Location
ForecastEx forecast contracts are sorted into a category hierarchy for organizational purposes.
These categories are metadata rather than immutable attributes of the tradable instruments themselves – they can be expected to change slightly over time.
This category tree is three levels deep, and its leaves (the level 3 categories) contain the forecast contracts “Markets” — groups of tradable contracts sharing questions of the same form e.g. “will the Fed Funds rate on X date exceed Y percent”.
The
/forecast/category/tree
endpoint can be used to retrieve the complete category tree.
Returns event contract category and market tree
GET /forecast/category/tree
Request Object
No Body Params
Python
cURL
import requests

url = "{{base-url}}/forecast/category/tree"

payload = {}
headers = {}

response = requests.request("GET", url, headers=headers, data=payload)

print(response.text)
curl --location --globoff '{{base-url}}/forecast/category/tree'
Response Object
categories:
List
List of categories
id:
String
Category identifier
name:
String
Category name
parent_id:
String
Identifier or parent category, optional
markets:
List
List of markets, optional
name:
String
Market name
symbol:
String
Market symbol
exchange:
String
Market exchange
conid:
Integer
Market contract identifier
as_of:
String
Timestamp of data retrieval
{
    "categories": {
        "g78664": {
            "name": "Northeast",
            "parent_id": "g17457",
            "markets": [
                {
                    "name": "Northeastern US CPI",
                    "symbol": "RCNET",
                    "exchange": "FORECASTX",
                    "conid": 831072285,
                    "product_conid": 831072289
                }
            ]
        },
}
Markets and Strikes
Copy Location
ForecastEx forecast contracts are modeled as options or futures options, depending on the event they resolve against.
Because they are derivative products, they are always listed against an underlier. Presently forecast contract underliers are either an index or futures contract. The underliers will have their own contract IDs separate from the contract IDs of the forecast contracts.
These underlier contract IDs can be used to retrieve relevant historical data sets for the underlying event, where available. For example, the GT (Global Temperature) contracts are listed against a GT index, and the index data set is historical global temperature data sourced from NOAA.
In all cases, a Market has a symbol, mirroring options. Examples: FF, HORC, USIP
Forecast contracts, like options, have a strike and expiration. Strike value need not be numeric; for instance, for election-related contracts it will be a candidate’s name. The strikeLabel field in the
/contracts
response delivers these strings.
All contracts have a true expiration which is the resolution time mentioned above after which the contract is considered resolved and it ceases to exist.
A fully specified question, including the strike value and measured period, is referred to as a “strike” – similar to a specific strike row in a two-sided option chain table.
Each such strike has two contracts associated with it: YES and NO.
IBKR assigns a separate contract ID to both the YES and NO contracts of a given strike.
Following from the options model: YES is a Call, and NO is a Put.
For each contract:
The long (and canonical) form of this question is delivered in the longDescription field.
A shortened options-style form is delivered in the shortDescription field.
Note that YES and NO contracts each have their own bid/ask/last data.
Provides all contracts for given underlyin market
GET /forecast/contract/market
Request Object
Query Params
underlyingConid:
Integer
name of contract’s market
exchange:
String
Python
cURL
import requests

url = "{{base-url}}/forecast/contract/market?underlyingConid=766914406&exchange=FORECASTX"

payload = {}
headers = {}

response = requests.request("GET", url, headers=headers, data=payload)

print(response.text)
curl --location --globoff '{{base-url}}/forecast/contract/market?underlyingConid=766914406&exchange=FORECASTX'
Response Object
market_name:
String
Name of contract’s market
exchange:
String
Exchange that was passed in request
symbol:
String
Market symbol
logo_category:
String
exclude_historical_data:
Bool
payout:
Double
contracts:
List
conid:
Integer
Market contract identifier
side:
String
Y or N, yes or no contract
expiration:
String
Contract expiration date in YYYYMMDD format
strike:
Double
Contract strike
strike_label:
String
expiry_label:
String
underlying_conid
Integer
Underlying asset of the contract
{
    "market_name": "Georgia Governor Democratic Primary",
    "exchange": "FORECASTX",
    "symbol": "GPGAD",
    "logo_category": "g17467",
    "exclude_historical_data": true,
    "payout": 1.0,
    "contracts": [
        {
            "conid": 767285167,
            "side": "Y",
            "expiration": "20260612",
            "strike": 1.0,
            "strike_label": "Stacey Abrams",
            "expiry_label": "2026",
            "underlying_conid": 766914406,
            "time_specifier": "2026.5.19"
        },
}
Contract Rules
Copy Location
Provides contract rules for specific binary options.
GET /forecast/contract/rules
Request Object
Query Params
conid:
Integer
Contract identifier
Python
cURL
import requests

url = "{{base-url}}/forecast/contract/rules?conid=767285167"

payload = {}
headers = {}

response = requests.request("GET", url, headers=headers, data=payload)

print(response.text)
curl --location --globoff '{{base-url}}/forecast/contract/rules?conid=767285167'
Response Object
asset_class:
String
Product asset class
description:
String
Product description
market_name:
String
Name of contract’s market
measured_period:
String
threshold:
String
Either strike or strike label depending on the contract
source_agency:
String
Name of source agency
data_and_resolution_link:
String
Link to data from source agency
last_trade_time:
Long
Last trade time in EPOCH
product_code:
String
Product code, symbo
market_rules_link:
String
Link to market rules document
release_time:
Long
Release time in EPOCH seconds
payout_time:
Long
Payout time in EPOCH seconds
payout:
String
Formatted payout amount
price_increment:
String
Formatted price increment amount
exchange_timezone:
String
Exchange timezone
{
    "asset_class": "OPT",
    "description": "The Georgia Democratic Gubernatorial Primary determines the party nominee for governor, shaping state leadership and national political influence.",
    "market_name": "Georgia Governor Democratic Primary",
    "measured_period": "May19'26",
    "threshold": "Stacey Abrams",
    "source_agency": "Georgia Secretary of State Elections Division",
    "data_and_resolution_link": "https://sos.ga.gov/index.php/elections",
    "last_trade_time": 1781301540,
    "product_code": "GPGAD",
    "market_rules_link": "https://data.forecastex.com/regulatory/GPTermsandConditions.pdf",
    "release_time": 1781301540,
    "payout_time": 1781373600,
    "payout": "$1.00",
    "price_increment": "$0.01",
    "exchange_timezone": "US/Central"
}
Trading schedule
Copy Location
Provides contract trading schedules
GET /forecast/contract/schedules
Request Object
Query Params
conid:
Integer
Contract identifier
Python
cURL
import requests

url = "{{base-url}}/forecast/contract/schedules?conid=767285167"

payload = {}
headers = {}

response = requests.request("GET", url, headers=headers, data=payload)

print(response.text)
curl --location --globoff '{{base-url}}/forecast/contract/schedules?conid=767285167'
Response Object
timezone:
String
Exchange timezone
trading schedule:
List
List of strikes
day_of_week:
String
trading_times:
List
List of trading time intervalse
open:
String
Start of trading interval
close:
String
End of trading interval
{
    "timezone": "US/Central",
    "trading_schedules": [
        {
            "day_of_week": "Saturday",
            "trading_times": [
                {
                    "open": "12:00 AM",
                    "close": "4:15 PM"
                },
                {
                    "open": "4:16 PM",
                    "close": "11:59 PM"
                }
            ]
        },
        {
            "day_of_week": "Sunday",
            "trading_times": [
                {
                    "open": "12:00 AM",
                    "close": "4:15 PM"
                },
                {
                    "open": "4:16 PM",
                    "close": "11:59 PM"
                }
            ]
        },
}
Contract details
Copy Location
Provides contract rules for specific event binary options
GET /forecast/contract/details
Request Object
Query Params
conid:
Integer
Contract identifier
Python
cURL
import requests

url = "{{base-url}}/forecast/contract/details?conid=767285167"

payload = {}
headers = {}

response = requests.request("GET", url, headers=headers, data=payload)

print(response.text)
curl --location --globoff '{{base-url}}/forecast/contract/details?conid=767285167'
Response Object
conid_yes:
Integer
Contract id of “yes” contract
conid_no:
Integer
Contract id of “no” contract
question:
String
Contract question(i.e “Will this happen on this date?”)
side:
String
“Y” or “N” – yes or no contract
strike_label:
String
Strike label to display
strike:
Double
Contract strike
exchange:
String
Contract exchange
expiration:
String
Contract expiration
symbol:
String
Contract symbol
logo_category:
String
measured_period:
market_name:
String
Name of contract’s market
unerlying_conid:
Integer
Underlying asset of the contract
{
    "conid_yes": 767285167,
    "conid_no": 767285169,
    "question": "Will Stacey Abrams win the Georgia Democratic primary for governor in 2026?",
    "side": "Y",
    "strike_label": "Stacey Abrams",
    "strike": 1.0,
    "exchange": "FORECASTX",
    "expiration": "20260612",
    "symbol": "GPGAD",
    "category": "g7428",
    "logo_category": "g17467",
    "measured_period": "May19'26",
    "market_name": "Georgia Governor Democratic Primary",
    "underlying_conid": 766914406,
    "payout": 1.0
}
Order Submission
Copy Location
Submission of orders for Event Contracts via the Web API functions like
orders for any other instrument
.
However, it is important to note the differing mechanics between CME Group products and ForecastEx instruments:
CME Group instruments can be bought and sold and function as normal futures options.
ForecastEx instruments cannot be sold, only bought. To exit or reduce a position, one must buy the opposing Event Contract, and IB will net the opposing positions together automatically.
In both cases, no short selling is permitted.
ForecastEx
In the context of ForecastEx trading, the other right must be bought to open and close the position. In this scenario, we are entering the position by buying the YES side of the January contract. This is demonstrated in the first payload.
However, if we wanted to close the position after buying the YES, we would use the second payload to buy the NO side.
Python
cURL
import requests

url = f"{baseUrl}/iserver/account/{accountId}/orders"

// Opening Order (BUY to open 1 YES contract) 
payload = {
  "orders": [
    {
      "side": "BUY",
      "quantity": 1,
      "conid": 773659700,             //YES contract expiring JAN 04 '27
      "orderType": "MKT",
      "listingExchange": "FORECASTEX",
      "tif": "DAY"
    }
  ]
}

// Closing Order (BUY to close 1 NO contract)
payload = {
  "orders": [
    {
      "side": "BUY",
      "quantity": 1,
      "conid": 773659707,             //NO contract expiring JAN 04 '27
      "orderType": "MKT",
      "listingExchange": "FORECASTEX",
      "tif": "DAY"
    }
  ]
}

response = requests.post(url=url, data=payload)
print(response.text)
curl \
--url {baseUrl}/iserver/account/{accountId}/orders \
--request POST \
--header 'Content-Type:application/json' \
--data '{
  "orders": [
    {
      "side": "BUY",
      "quantity": 1,
      "conid": 773659700,
      "orderType": "MKT",
      "listingExchange": "FORECASTEX",
      "tif": "DAY"
    }
  ]
}'
CME Group
In the case of CME listed contracts, we can simply change our
side
value from BUY to SELL to close our position.
Python
cURL
import requests

url = f"{baseUrl}/iserver/account/{accountId}/orders"

// Closing Order (BUY to close 1 NO contract)
payload = {
  "orders": [
    {
      "side": "BUY",
      "quantity": 1,
      "conid": 861917649,       // US Payroll Employment 
      "orderType": "MKT",
      "listingExchange": "CME",
      "tif": "DAY"
    }
  ]
}

response = requests.post(url=url, data=payload)
print(response.text)
curl \
--url {baseUrl}/iserver/account/{accountId}/orders \
--request POST \
--header 'Content-Type:application/json' \
--data '{
  "orders": [
    {
      "side": "BUY",
      "quantity": 1,
      "conid": 861917649,       //US Payroll Employment
      "orderType": "MKT",
      "listingExchange": "CME",
      "tif": "DAY"
    }
  ]
}'
Executions and Netting
Copy Location
Positions in forecast contracts are opened by buying either a YES or NO contract. Positions are reduced or closed by buying the opposite contract at the same strike: NO reduces YES, and YES reduces NO.
An opening order will receive normal Bought/Bot execution reports (“side”: “B”). However, if an account is already long YES or NO, a reduction of that position will produce a series of execution reports.
For example, consider an account long 100 YES contracts. A new order is submitted to buy 10 NO contracts of the same strike. Execution of this Buy NO order will be netted against the long 100 YES position.
Order receives an execution for 10 NO contracts.
IB sends an execution report of Bot 10 NO (“side”: “B”). This momentarily creates a long 10 NO position in the account.
IB sends an execution report of Netting 10 YES (“side”: “N”). This reduces the long 100 YES position in the account by 10, yielding 90.
IB sends an execution report of Netting 10 NO (“side”: “N”). This reduces the long 10 NO position by 10 to 0, flattening it.
Net result: Account contains only the long 90 YES position.
The above netting execution reports will arrive within milliseconds of the first Bot execution. IB’s ForecastTrader reflects all such Bot & Netting executions as separate trades in the account’s Trade History.
Note that it is also possible to change the side of a position from YES to NO via a single opposite-side buy order.
For example, consider the same account, now long 90 YES. Another order is submitted to buy 130 NO contracts on the same strike.
Order receives an execution for 130 NO contracts.
IB sends an execution report of Bot 130 NO (“side”: “B”). This momentarily creates a long 130 NO position in the account.
IB sends an execution report of Netting 90 YES (“side”: “N”). This reduces the long 90 YES position in the account to 0, flattening it.
IB sends an execution report of Netting 90 NO (“side”: “N”). This reduces the the long 130 NO position by 90, yielding 40.
Net result: Account contains only the long 40 NO position.
FA Allocation Management
Copy Location
Allocatable Sub-Accounts
Copy Location
Retrieves a list of all sub-accounts and returns their net liquidity and available equity for advisors to make decisions on what accounts should be allocated and how.
GET /iserver/account/allocation/accounts
Request Object
No params or body content should be sent.
Python
cURL
request_url = f"{baseUrl}/iserver/account/allocation/accounts" 
requests.get(url=request_url)
curl \
--url {{baseUrl}}/iserver/account/allocation/accounts \
--request GET
Response Object
accounts:
Array of objects.
Array containing all sub-accounts held by the advisor.
[{
data:
Array of objects.
Contains Net liquidation and available equity of the given account Id.
[{
value:
String.
Contains the price value affiliated with the key.
key:
String.
Defines the value of the object.
Expected values: “AvailableEquity”, “NetLiquidation”
}]
name:
String.
Returns the account ID affiliated with the balance data.
}]
{
  "accounts": [
    {
      "data": [
        {
          "value": "2677.89",
          "key": "NetLiquidation"
        },
        {
          "value": "2134.76",
          "key": "AvailableEquity"
        }
      ],
      "name": "U123456"
    },
    {
      "data": [
        {
          "value": "1200.88",
          "key": "NetLiquidation"
        },
        {
          "value": "1000.56",
          "key": "AvailableEquity"
        }
      ],
      "name": "U456789"
    }
  ]
}
List All Allocation Groups
Copy Location
Retrieves a list of all of the advisor’s allocation groups. This describes the name of the allocation group, number of subaccounts within the group, and the method in use for the group.
GET /iserver/account/allocation/group
Request Object
No params or body content should be sent.
Python
cURL
request_url = f"{baseUrl}/iserver/account/allocation/group" 
requests.get(url=request_url)
curl \
--url {{baseUrl}}/iserver/account/allocation/group \
--request GET
Response Object
data:
Array of objects.
Contains object pairs for each allocation groups
[{
allocation_method:
String.
Uses the Allocation Method Code to represent which method is implemented.
size:
int.
Represents the total number of sub-accounts within the group.
name:
String.
The name set for the given allocation group.
}]
{
  "data": [
    {
      "allocation_method": "N",
      "size": 10,
      "name": "Group_1_NetLiq"
    }
  ]
}
Retrieve Single Allocation Group
Copy Location
Retrieves the configuration of a single account group. This describes the name of the allocation group, the specific accounts contained in the group, and the allocation method in use along with any relevant quantities.
POST /iserver/account/allocation/group/single
Request Object
Body Params
name:
String. Required.
Name of an existing allocation group.
Python
cURL
request_url = f"{baseUrl}/iserver/account/allocation/group/single" 
json_content ={
  "name":"Group_1_NetLiq"
}
requests.post(url=request_url, json=json_content)
curl \
--url {{baseUrl}}/iserver/account/allocation/group/single \
--request POST
--header 'Content-Type:application/json' \
--data '{"name":"Group_1_NetLiq"}'
Response Object
{
name:
String. Required Required
Name used to refer to your allocation group. This will be used while placing orders.
accounts:
Array of objects. Required
Contains a series of objects depicting which accounts are involved and, for user-defined allocation methods, the distribution value for each sub-account.
[
{
name:
String. Required
The accountId of a given sub-account.
Value Format: “U1234567”
amount:
Number.
The total distribution value for each sub-account for user-defined allocation methods.
}
]
default_method:
String.
Specify the allocation method code for the allocation group.
See Allocation Method Codes for more details.
}
{
  "name": "Group_1_NetLiq",
  "accounts": [
    {
      "amount": 1,
      "name": "DU1234567"
    },
    {
      "amount": 5,
      "name": "DU9876543"
    }
  ],
  "default_method": "R"
}
Add Allocation Group
Copy Location
Add a new allocation group. This group can be used to trade
POST /iserver/account/allocation/group
Request Object
Body Params
name:
String. Required
Required
Name used to refer to your allocation group. This will be used while placing orders.
accounts:
Array of objects.
Required
Contains a series of objects depicting which accounts are involved and, for user-defined allocation methods, the distribution value for each sub-account.
[{
name:
String.
Required
The accountId of a given sub-account.
Value Format: “U1234567”
amount:
Number.
The total distribution value for each sub-account for user-defined allocation methods.
}]
default_method:
String.
Specify the allocation method code for the allocation group.
See Allocation Method Codes for more details.
Python
cURL
request_url = f"{baseUrl}/iserver/account/allocation/group"
json_content = {
  "name":"Group_1_NetLiq",
  "accounts":[{
    "name":"U1234567",
    "amount":10
  },{
    "name":"U2345678",
    "amount":5
  }],
  "default_method":"N"
}
requests.post(url=request_url, json=json_content
curl \
--url {{baseUrl}}/iserver/account/allocation/group \
--request POST
--header 'Content-Type:application/json' \
--data '{
  "name":"Group_1_NetLiq",
  "accounts":[{
    "name":"U1234567",
    "amount":10
  },{
    "name":"U2345678",
    "amount":5
  }],
  "default_method":"N"
}'
Response Object
success:
bool.
Confirms that the allocation group was properly set.
{
  "success": true
}
Modify Allocation Group
Copy Location
Modify an existing allocation group.
PUT /iserver/account/allocation/group
Request Object
Body Params
name:
String. Required
Required
Name used to refer to your allocation group. If prev_name is specified, this will become the new name of the group.
prev_name:
String.
Name used to refer to your existing allocation group.
Only use this when updating the group name.
accounts:
Array of objects.
Required
Contains a series of objects depicting which accounts are involved and, for user-defined allocation methods, the distribution value for each sub-account.
[{
name:
String.
Required
The accountId of a given sub-account.
Value Format: “U1234567”
amount:
Number.
The total distribution value for each sub-account for user-defined allocation methods.
}]
default_method:
String.
Required
Specify the allocation method code for the allocation group.
See Allocation Method Codes for more details.
Python
cURL
request_url = f"{baseUrl}/iserver/account/allocation/group"
json_content = {
  "name":"Group_1_NetLiq",
  "accounts":[{
    "name":"U1234567",
    "amount":15
  },{
    "name":"U2345678",
    "amount":10
  }],
  "default_method":"N"
}
requests.put(url=request_url, json=json_content
curl \
--url {{baseUrl}}/iserver/account/allocation/group \
--request PUT
--header 'Content-Type:application/json' \
--data '{
  "name":"new_test_group",
  "prev_name":"Group_1_NetLiq",
  "accounts":[{
    "name":"U1234567",
    "amount":10
  },{
    "name":"U2345678",
    "amount":5
  }],
  "default_method":"A"
}'
Response Object
success:
bool.
Confirms that the allocation group was properly set.
{
  "success": true
}
Delete Allocation Group
Copy Location
Remove an existing allocation group. This group will no longer be accessible.
POST /iserver/account/allocation/group/delete
Request Object
Body Params
name:
String. Required
Required
Name used to refer to your allocation group.
Python
cURL
request_url = f"{baseUrl}/iserver/account/allocation/group/delete"
json_content = {
  "name":"Group_1_NetLiq",
}
requests.post(url=request_url, json=json_content
curl \
--url {{baseUrl}}/iserver/account/allocation/group/delete \
--request POST
--header 'Content-Type:application/json' \
--data '{
  "name":"Group_1_NetLiq",
}'
Response Object
success:
bool.
Confirms that the allocation group was properly set.
{
  "success": true
}
Retrieve Allocation Presets
Copy Location
Retrieve the preset behavior for allocation groups for specific events.
GET /iserver/account/allocation/presets
Request Object
No params or body content should be sent.
Python
cURL
request_url = f"{baseUrl}/iserver/marketdata/unsubscribeall"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/iserver/account/allocation/presets \
--request GET
Response Object
group_auto_close_positions: bool.
default_method_for_all: String.
profiles_auto_close_positions: bool.
strict_credit_check: bool.
group_proportional_allocation: bool.
{
  "group_auto_close_positions": false,
  "default_method_for_all": "N",
  "profiles_auto_close_positions": false,
  "strict_credit_check": false,
  "group_proportional_allocation": false
}
Set Allocation Presets
Copy Location
Set the preset behavior for allocation groups for specific events.
POST /iserver/account/allocation/presets
Request Object
Body Params
default_method_for_all:
String.
Required
Set the default allocation method to be used for all allocation groups without a set value.
group_auto_close_positions:
bool.
Required
profiles_auto_close_positions:
bool.
Required
strict_credit_check:
bool.
Required
group_proportional_allocation:
bool.
Required
Python
cURL
request_url = f"{baseUrl}/iserver/account/allocation/presets" 
json_content = {
  "default_method_for_all": "E",
  "group_auto_close_positions": true,
  "profiles_auto_close_positions": true,
  "strict_credit_check": false,
  "group_proportional_allocation": false
}
requests.post(url=request_url, json=json_content)
curl \
--url {{baseUrl}}/iserver/account/allocation/presets \
--request POST \
--header 'Content-Type:application/json' \
--data '{
  "default_method_for_all": "E",
  "group_auto_close_positions": true,
  "profiles_auto_close_positions": true,
  "strict_credit_check": false,
  "group_proportional_allocation": false
}'
Response Object
success:
bool.
Confirms that the preset was properly set.
{
  "success": true
}
Allocation Method Codes
Copy Location
Interactive Brokers supports two forms of allocation methods. Allocation methods that have calculations completed by Interactive Brokers, and a set of allocation methods calculated by the user and then specified.
IB-computed allocation methods
Method
Code
Available Equity
A
Equal
E
Net Liquidation Value
N
User-specified allocation methods
Formerly known as Allocation Profiles
Method
Code
Cash Quantity
C
Percentages
P
Ratios
R
Shares
S
Allocation Preset Combinations
Copy Location
In order to attain specific allocation behaviors, a combination of various settings must be specified. The tables below detail what settings must be used.Interactive Brokers supports two forms of allocation methods. Allocation methods that have calculations completed by Interactive Brokers, and a set of allocation methods calculated by the user and then specified.
The preset settings are based on the Advisor Presets setting built in TWS.
Every time a user logs in to TWS, the presets established in the CPAPI will update to reflect the settings in TWS.
Presets adjusted in the Client Portal API will not adjust the settings in TWS.
IB-computed allocation methods
Intended Behavior
Proportional Allocation
Closing Behavior
Make positions be proportional based on method
group_proportional_allocation=false
group_auto_close_positions=true
Distribute shares based on method selected
group_proportional_allocation=true
group_auto_close_positions=true
Distribute shares based on method selected, do not prioritize accounts that are closing position
group_proportional_allocation=true
group_auto_close_positions=false
User-specified allocation methods
Formerly known as Allocation Profiles
Intended Behavior
Closing Behavior
Distribute shares based on method selected
profile_auto_close_positions=true
Distribute shares based on method selected, do not prioritize accounts that are closing position
profile_auto_close_positions=false
FYIs and Notifications
Copy Location
Unread Bulletins
Copy Location
Returns the total number of unread fyis
GET /fyi/unreadnumber
Request Object
No params or body content should be sent.
Python
cURL
request_url = f"{baseUrl}/fyi/unreadnumber"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/fyi/unreadnumber \
--request GET
Response Object
BN:
int.
Returns the number of unread bulletins.
{
  "BN": 4
}
Get a List of Subscriptions
Copy Location
Return the current choices of subscriptions for notifications.
GET /fyi/settings
Request Object
No params or body content should be sent.
Python
cURL
request_url = f"{baseUrl}/fyi/settings"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/fyi/settings \
--request GET
Response Object
A:
int.
Returns if the subscription can be modified.
Only returned if the subscription can be modified.
See /fyi/settings/{typecode} for how to modify.
FC:
String.
Fyi code for enabling or disabling the notification.
H:
int.
Disclaimer if the notification was read.
Value Format: 0: Unread; 1: Read
FD:
String.
Returns a detailed description of the topic.
FN:
String.
Returns a human readable title for the notification.
[
  {
    "FC": "M8",
    "H": 0,
    "A": 1,
    "FD": "Notify me when I establish position subject to US dividend tax withholding 871(m) rules.",
    "FN": "871(m) Trades"
  },
  {
    "FC": "AA",
    "H": 0,
    "A": 1,
    "FD": "Notifications related to account activity such as funding, application, trading and market data permission status",
    "FN": "Account Activity"
  },
  {...}
]
Enable/Disable Specified Subscription
Copy Location
Configure which typecode you would like to enable/disable.
POST /fyi/settings/{{ typecode }}
Request Object
Path Params
typecode:
String.
Required
Code used to signify a specific type of FYI template.
See
Typecode
section for more details.
Body Params
enabled:
bool.
Required
Enable or disable the subscription.
See available typecodes under
FYI Typecodes
Value format: true: Enable; false: Disable
Python
cURL
request_url = f"{baseUrl}/fyi/settings/SM"
json_content ={"enabled":true}
requests.post(url=request_url, json=json_content)
curl \
--url {{baseUrl}}/fyi/settings/SM \
--request POST \
--data '{"enabled":true}'
Response Object
V:
int.
Returns 1 to state message was acknowledged.
T:
int.
Returns the time in ms to complete the edit.
{
  "V": 1,
  "T": 10
}
FYI Typecodes
Copy Location
Many FYI endpoints reference a “typecode” value. The table below lists the available codes and what they correspond to.
Typecode
Description
BA
Borrow Availability
CA
Comparable Algo
DA
Dividends Advisory
EA
Upcoming Earnings
MF
Mutual Fund Advisory
OE
Option Expiration
PR
Portfolio Builder Rebalance
SE
Suspend Order on Economic Event
SG
Short Term Gain turning Long Term
SM
System Messages
T2
Assignment Realizing Long-Term Gains
TO
Takeover
UA
User Alert
M8
M871 Trades
PS
Platform Use Suggestions
DL
Unexercised Option Loss Prevention Reminder
PT
Position Transfer
CB
Missing Cost Basis
MS
Milestones
TD
MiFID || 10% Deprecation Notice
ST
Save Taxes
TI
Trade Idea
CT
Cash Transfer
Get disclaimer for a certain kind of fyi
Copy Location
Receive additional disclaimers based on the specified typecode.
GET /fyi/disclaimer/{typecode}
Request Object
Path Params
typecode:
String.
Required
Code used to signify a specific type of FYI template.
See
FYI Typecodes
section for more details.
Python
cURL
request_url = f"{baseUrl}/fyi/disclaimer/SM"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/fyi/disclaimer/SM\
--request GET
Response Object
FC:
String.
Returns the Typecode for the given disclaimer.
DT:
String.
Returns the Disclaimer message
{
  "FC": "SM",
  "DT": "This communication is provided for information purposes only and is not intended as a recommendation or a solicitation to buy, sell or hold any investment product. Customers are solely responsible for their own trading decisions."
}
Mark Disclaimer Read
Copy Location
Mark disclaimer message read.
PUT /fyi/disclaimer/{typecode}
Request Object
Path Params
typecode:
String.
Required
Code used to signify a specific type of FYI template.
See
Typecode
section for more details.
Python
cURL
request_url = f"{baseUrl}/fyi/disclaimer/CT"
json_content = {}
requests.put(url=request_url, json=json_content
curl \
--url {{baseUrl}}/fyi/disclaimer/CT \
--request PUT \
--data ''
Response Object
V:
int.
Returns 1 to state message was acknowledged.
T:
int.
Returns the time in ms to complete the edit.
{
  "V": 1,
  "T": 10
}
Get Delivery Options
Copy Location
Options for sending fyis to email and other devices
GET /fyi/deliveryoptions
Request Object
No params or body content should be sent.
Python
cURL
request_url = f"{baseUrl}/fyi/deliveryoptions"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/fyi/deliveryoptions \
--request GET
Response Object
M:
int.
Email option is enabled or not.
Value Format: 0: Email Disabled; 1: Email Enabled.
E:
Array.
Returns an array of device information.
[{
NM:
String.
Returns the human readable device name.
I:
String.
Returns the deice identifier.
UI:
String.
Returns the unique device ID.
A:
String.
Device is enabled or not.
Value Format: 0: Disabled; 1: Enabled.
}]
{
  "E": [
    {
      "NM": "iPhone",
      "I": "apn://mtws@1234E5E67D8A9012EC3E45D6E7D89A01F2345CDBBB678B9BE0FB12345AF6D789",
      "UI": "apn://mtws@1234E5E67D8A9012EC3E45D6E7D89A01F2345CDBBB678B9BE0FB12345AF6D789",
      "A": 1
    }
  ],
  "M": 1
}
Enable/Disable Device Option
Copy Location
Choose whether a particular device is enabled or disabled.
POST /fyi/deliveryoptions/device
Request Object
Body Params
devicename:
String. Required
Human readable name of the device.
deviceId:
String. Required
ID Code for the specific device.
uiName:
String. Required
Title used for the interface system.
enabled:
bool. Required
Specify if the device should be enabled or disabled.
Python
cURL
request_url = f"{baseUrl}/fyi/deliveryoptions/device"
json_content = {
    "deviceName": "iPhone",
    "deviceId": "apn://mtws@1234E5E67D8A9012EC3E45D6E7D89A01F2345CDBBB678B9BE0FB12345AF6D789",
    "uiName": "apn://mtws@1234E5E67D8A9012EC3E45D6E7D89A01F2345CDBBB678B9BE0FB12345AF6D789",
    "enabled": True
}
requests.post(url=request_url, json=json_content)
curl \
--url {{baseUrl}}/fyi/deliveryoptions/device \
--request POST \
--data '{
    "deviceName": "iPhone",
    "deviceId": "apn://mtws@1234E5E67D8A9012EC3E45D6E7D89A01F2345CDBBB678B9BE0FB12345AF6D789",
    "uiName": "apn://mtws@1234E5E67D8A9012EC3E45D6E7D89A01F2345CDBBB678B9BE0FB12345AF6D789",
    "enabled": True
}'
Response Object
V:
int.
Returns 1 to state message was acknowledged.
T:
int.
Returns the time in ms to complete the edit.
{
  "V": 1,
  "T": 10
}
Delete a Device
Copy Location
Delete a specific device from our saved list of notification devices.
DELETE /fyi/deliveryoptions/{{ deviceId }}
Request Object
Path Params
deviceId:
String.
Required
Display the device identifier to delete from IB’s saved list.
Can be retrieved from
/fyi/deliveryoptions
.
Python
cURL
request_url = f"{baseUrl}/fyi/deliveryoptions/1" 
requests.delete(url=request_url)
curl \
--url {{baseUrl}}/fyi/deliveryoptions/1 \ 
--request DELETE
Response Object
No response message is returned. Instead, you will only receive an empty string with a 200 OK status code indicating a successfully deleted account.
Enable/Disable Email Option
Copy Location
Enable or disable your account’s primary email to receive notifications.
PUT /fyi/deliveryoptions/email
Request Object
Query Params
enabled:
String.
Required
Enable or disable your email.
Value format: true: Enable; false: Disable
Python
cURL
request_url = f"{baseUrl}/fyi/deliveryoptions/email?enabled=true"
json_content = {}
requests.put(url=request_url, json=json_content)
curl \
--url {{baseUrl}}/fyi/deliveryoptions/email?enabled={{ enabled }} \
--request PUT \
--data ""
Response Object
V:
int.
Returns 1 to state message was acknowledged.
T:
int.
Returns the time in ms to complete the edit.
{
  "V": 1,
  "T": 10
}
Get a list of notifications
Copy Location
Get a list of available notifications.
GET /fyi/notifications
Request Object
Query Params
max:
String.
Specify the maximum number of notifications to receive.
Can request a maximum of 10 notifications.
Python
cURL
request_url = f"{baseUrl}/fyi/notifications?max=10" 
requests.get(url=request_url)
curl \
--url {{baseUrl}}/fyi/notifications?max=10 \ 
--request GET
Response Object
D:
String.
Notification date
ID:
String.
Unique way to reference the notification.
FC:
String.
FYI code, we can use it to find whether the disclaimer is accepted or not in settings
MD:
String.
Content of notification.
MS:
String.
Title of notification.
R:
string.
Return if the notification was read or not.
Value Format: 0: Disabled; 1: Enabled.
[{
  "R": 0,
  "D": "1702469440.0",
  "MS": "IBKR FYI: Option Expiration Notification",
  "MD": "One or more option contracts in your portfolio are set to expire shortly.
- QQQ 15DEC2023 385 P in Account(Qty): U****7890(6)
- QQQ 15DEC2023 387 P in Account(Qty): D****0685(-6)
Please use the Option Rollover tool to roll existing contracts into contracts with an expiration, strike and price condition of your preference.",
  "ID": "2023121370119463",
  "HT": 0,
  "FC": "OE"
}]
Mark Notification Read
Copy Location
Mark a particular notification message as read or unread.
PUT /fyi/notifications/{notificationID}
Request Object
Path Params
notificationId:
String.
Required
Code used to signify a specific notification to mark.
Python
cURL
request_url = f"{baseUrl}/fyi/notifications/more?id=12345678901234567"
json_content = {}
requests.put(url=request_url, json=json_content)
curl \
--url {{baseUrl}}/fyi/notifications/12345678901234567 \
--request PUT \
--data ""
Response Object
V:
int.
Returns 1 to state message was acknowledged.
T:
int.
Returns the time in ms to complete the edit.
P:
Object.
Returns details about the notification read status.
R:
int.
Returns if the message was read (1) or unread (0).
ID:
String.
Returns the ID for the notification..
{
  "V": 1,
  "T": 5,
  "P": {
    "R": 1,
    "ID": "12345678901234567"
  }
}
Market Data
Copy Location
Live Market Data Snapshot
Copy Location
Get Market Data for the given conid(s).
A pre-flight request must be made prior to ever receiving data. For some fields, it may take more than a few moments to receive information.
See response fields for a list of available fields that can be request via fields argument.
The endpoint /iserver/accounts must be called prior to /iserver/marketdata/snapshot.
For derivative contracts the endpoint /iserver/secdef/search must be called first.
GET /iserver/marketdata/snapshot
Request Object
Query Parameters
conids:
String.
Required
Contract identifier for the contract of interest. A maximum of 100 conids may be specified.
May provide a comma-separated series of contract identifiers.
fields:
String.
Required
Specify a series of tick values to be returned. A maximum of 50 fields may be specified.
May provide a comma-separated series of field ids.
See
Market Data Fields
for more information.
Python
cURL
request_url = f"{baseUrl}/iserver/marketdata/snapshot?conids=265598,8314&fields=31,84,86"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/iserver/marketdata/snapshot?conids=265598,8314&fields=31,84,86 \ 
--request GET
Response Object
server_id:
String.
Returns the request’s identifier.
conidEx:
String.
Returns the passed conid field. May include exchange if specified in request.
conid:
int.
Returns the contract id of the request
_updated:
int*.
Returns the epoch time of the update in a 13 character integer .
6119:
String.
Field value of the server_id. Returns the request’s identifier.
fields*:
String.
Returns a response for each request. Some fields not be as readily available as others. See the Market Data section for more details.
6509:
String.
Returns a multi-character value representing the
Market Data Availability.
[
  {
    "_updated": 1702334859712,
    "conidEx": "265598",
    "conid": 265598,
    "server_id": "q1",
    "6119": "serverId",
    "31": "193.18",
    "84": "193.06",
    "86":"193.14", 
    "6509": "RpB"
  }
]
Market Data Update Frequency
Copy Location
Watchlist market data at Interactive Brokers is derived from time-based snapshot intervals which vary by product and region. This means that a given tick will only update as frequently as its interval allows. See the table for more details on product specifics.
Please keep in mind that the Web API still retains a standard pacing limit of 10 requests per second. For more frequency returns, implement the
smd websocket topic
in place of the HTML endpoint.
Product
Frequency
All Products
500ms
Regulatory Snapshot
Copy Location
WARNING:
Each regulatory snapshot made
will incur a fee of $0.01 USD
to the account.
This applies to both live and paper accounts.
If you are already paying for, or are subscribed to, a specific US Network subscription, your account will not be charged.
See
here
for more information about Regulatory Snapshots and Market Data.
Send a request for a regulatory snapshot.
This will cost $0.01 USD per request
unless you are subscribed to the direct exchange market data already.
GET /md/regsnapshot
Request Object
Query Params
conid:
String.
Required
Provide the contract identifier to retrieve market data for.
Python
cURL
request_url = f"{baseUrl}/md/regsnapshot?conid=265598"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/md/regsnapshot?conid=265598 \
--request GET
Response Object
Note:
The integer fields returned below also correspond to the
Market Data Field
values used for the standard /iserver/marketdata/snapshot endpoint.
conid:
int.
Returns the contract ID of the request.
conidEx:
String.
Returns the contract ID of the request type.
BboExchange:
String.
Color for Best Bid/Offer Exchange in hex code
HasDelayed:
false,
Returns if the data is live (false) or delayed (true).
84:
float.
Returns the Bid value.
86:
float.
Returns the Ask value.
88:
int.
Returns the Bid size.
85:
int.
Returns the Ask size.
BestBidExch:
int.
Returns the exchange identifier of the current best bid value.
Internal use only.
BestAskExch:
int.
Returns the exchange identifier of the current best Ask value.
Internal use only.
31:
float.
Returns the exchange identifier of the most recent Last value.
Internal use only.
7059:
int.
Returns the last traded size.
LastExch:
int.
Returns the exchange of the last exchange as a binary integer*
Internal use only.
7057:
String.
Returns the series of character codes for the Ask exchange.
7068:
String.
Returns the series of character codes for the Bid exchange.
7058:
String.
Returns the series of character codes for the Last exchange.
{
  "conid": conid,
  "conidEx": "conidEx",
  "BboExchange": "BboExchange",
  "HasDelayed": HasDelayed,
  "84": "Bid",
  "86": "Ask",
  "88": Bid_Size,
  "85": Ask_Size,
  "BestBidExch": BestBidExch,
  "BestAskExch": BestAskExch,
  "31": "Last",
  "7059": Last_Size,
  "LastExch": LastExch,
  "7057": "Ask Exch",
  "7068": "Bid Exch",
  "7058": "Last_Exch"
}
Market Data Availability
Copy Location
The field may contain three chars.
First character defines market data timeline. This includes:  R = RealTime, D = Delayed, Z = Frozen, Y = Frozen Delayed, N = Not Subscribed.
Second character defines the data structure. This includes: P = Snapshot, p = Consolidated.
Third character defines the type of data: This will always return: B = Book
Code
Name
Description
R
RealTime
Data is relayed back in real time without delay, market data subscription(s) are required.
D
Delayed
Data is relayed back 15-20 min delayed.
Z
Frozen
Last recorded data at market close, relayed back in real time.
Y
Frozen Delayed
Last recorded data at market close, relayed back delayed.
N
Not Subscribed
User does not have the required market data subscription(s) to relay back either real time or delayed data.
O
Incomplete Market Data API Acknowledgement
The annual Market Data API Acknowledgement has not been completed for the given user. To complete the agreement:
Log in to the
Select “Welcome” in the top right corner, and then “Settings”
You will find a large button for “Market Data Subscriptions” on the right.
Find the “Market Data API Agreement” on the right.
P
Snapshot
Snapshot request is available for contract.
p
Consolidated
Market data is aggregated across multiple exchanges or venues.
B
Book
Top of the book data is available for contract.
d
Performance Details Enabled
Additional performance details are available for this contract. Internal use intended.
Market Data Fields
Copy Location
Field
Return Type
Value
Description
31
string
Last Price
The last price at which the contract traded. May contain one of the following prefixes: C – Previous day’s closing price. H – Trading has halted.
55
string
Symbol
58
string
Text
70
string
High
Current day high price
71
string
Low
Current day low price
73
string
Market Value
The current market value of your position in the security. Market Value is calculated with real time market data (even when not subscribed to market data).
74
string
Avg Price
The average price of the position.
75
string
Unrealized PnL
Unrealized profit or loss. Unrealized PnL is calculated with real time market data (even when not subscribed to market data).
76
string
Formatted position
77
string
Formatted Unrealized PnL
78
string
Daily PnL
Your profit or loss of the day since prior close. Daily PnL is calculated with real time market data (even when not subscribed to market data).
79
string
Realized PnL
Realized profit or loss. Realized PnL is calculated with real time market data (even when not subscribed to market data).
80
string
Unrealized PnL %
Unrealized profit or loss expressed in percentage.
82
string
Change
The difference between the last price and the close on the previous trading day
83
string
Change %
The difference between the last price and the close on the previous trading day in percentage.
84
string
Bid Price
The highest-priced bid on the contract.
85
string
Ask Size
The number of contracts or shares offered at the ask price.
86
string
Ask Price
The lowest-priced offer on the contract.
87
string
Volume
Volume for the day, formatted with ‘K’ for thousands or ‘M’ for millions. For higher precision volume refer to field 7762.
88
string
Bid Size
The number of contracts or shares bid for at the bid price.
201
string
Right
Returns the right of the instrument, such as P for Put or C for Call.
6004
string
Exchange
6008
integer
Conid
Contract identifier from IBKR’s database.
6070
string
SecType
The asset class of the instrument.
6072
string
Months
6073
string
Regular Expiry
6119
string
Marker for market data delivery method (similar to request id)
6457
integer
Underlying Conid. Use /trsrv/secdef to get more information about the security
6508
string
Service Params.
6509
string
Market Data Availability. The field may contain three chars. First char defines: R = RealTime, D = Delayed, Z = Frozen, Y = Frozen Delayed, N = Not Subscribed, i – incomplete, v – VDR Exempt (Vendor Display Rule 603c). Second char defines: P = Snapshot, p = Consolidated. Third char defines: B = Book. RealTime
Data is relayed back in real time without delay, market data subscription(s) are required. Delayed – Data is relayed back 15-20 min delayed. Frozen – Last recorded data at market close, relayed back in real time. Frozen Delayed – Last recorded data at market close, relayed back delayed. Not Subscribed – User does not have the required market data subscription(s) to relay back either real time or delayed data. Snapshot – Snapshot request is available for contract. Consolidated – Market data is aggregated across multiple exchanges or venues. Book – Top of the book data is available for contract.
7051
string
Company name
7057
string
Ask Exch
Displays the exchange(s) offering the SMART price. A=AMEX, C=CBOE, I=ISE, X=PHLX, N=PSE, B=BOX, Q=NASDAQOM, Z=BATS, W=CBOE2, T=NASDAQBX, M=MIAX, H=GEMINI, E=EDGX, J=MERCURY
7058
string
Last Exch
Displays the exchange(s) offering the SMART price. A=AMEX, C=CBOE, I=ISE, X=PHLX, N=PSE, B=BOX, Q=NASDAQOM, Z=BATS, W=CBOE2, T=NASDAQBX, M=MIAX, H=GEMINI, E=EDGX, J=MERCURY
7059
string
Last Size
The number of unites traded at the last price
7068
string
Bid Exch
Displays the exchange(s) offering the SMART price. A=AMEX, C=CBOE, I=ISE, X=PHLX, N=PSE, B=BOX, Q=NASDAQOM, Z=BATS, W=CBOE2, T=NASDAQBX, M=MIAX, H=GEMINI, E=EDGX, J=MERCURY
7084
string
Implied Vol./Hist. Vol %
The ratio of the implied volatility over the historical volatility, expressed as a percentage.
7085
string
Put/Call Interest
Put option open interest/call option open interest for the trading day. Only available for Underlying contracts.
7086
string
Put/Call Volume
Put option volume/call option volume for the trading day.
7087
string
Hist. Vol. %
30-day real-time historical volatility.
7088
string
Hist. Vol. Close %
Shows the historical volatility based on previous close price.
7089
string
Opt. Volume
Option Volume
7094
string
Conid + Exchange
7184
string
canBeTraded
If contract is a trade-able instrument. Returns 1(true) or 0(false).
7219
string
Contract Description
7220
string
Contract Description
7221
string
Listing Exchange
7280
string
Industry
Displays the type of industry under which the underlying company can be categorized.
7281
string
Category
Displays a more detailed level of description within the industry under which the underlying company can be categorized.
7282
string
Average Volume
The average daily trading volume over 90 days.
7283
string
Option Implied Vol. %
A prediction of how volatile an underlying will be in the future.At the market volatility estimated for a maturity thirty calendar days forward of the current trading day, and based on option prices from two consecutive expiration months. To query the Implied Vol. % of a specific strike refer to field 7633.
7284
string
Historical volatility %
Deprecated, see field 7087
7285
string
Put/Call Ratio
7292
string
Cost Basis
Your current position in this security multiplied by the average price and multiplier.
7293
string
52 Week High
The highest price for the past 52 weeks.
7294
string
52 Week Low
The lowest price for the past 52 weeks.
7295
string
Open
Today’s opening price.
7296
string
Close
Today’s closing price.
7308
string
Delta
The ratio of the change in the price of the option to the corresponding change in the price of the underlying.
7309
string
Gamma
The rate of change for the delta with respect to the underlying asset’s price.
7310
string
Theta
A measure of the rate of decline the value of an option due to the passage of time.
7311
string
Vega
The amount that the price of an option changes compared to a 1% change in the volatility.
7607
string
Opt. Volume Change %
Today’s option volume as a percentage of the average option volume.
7633
string
Implied Vol. %
The implied volatility for the specific strike of the option in percentage. To query the Option Implied Vol. % from the underlying refer to field 7283.
7635
string
Mark
The mark price is, the ask price if ask is less than last price, the bid price if bid is more than the last price, otherwise it’s equal to last price.
7636
string
Shortable Shares
Number of shares available for shorting.
7637
string
Fee Rate
Interest rate charged on borrowed shares.
7638
string
Option Open Interest. Available for Underlyings and Call & Put options.
7639
string
% of Mark Value
Displays the market value of the contract as a percentage of the total market value of the account. Mark Value is calculated with real time market data (even when not subscribed to market data).
7644
string
Shortable
Describes the level of difficulty with which the security can be sold short.
7671
string
Dividends
This value is the total of the expected dividend payments over the next twelve months per share.
7672
string
Dividends TTM
This value is the total of the expected dividend payments over the last twelve months per share.
7674
string
EMA(200)
Exponential moving average (N=200).
7675
string
EMA(100)
Exponential moving average (N=100).
7676
string
EMA(50)
Exponential moving average (N=50).
7677
string
EMA(20)
Exponential moving average (N=20).
7678
string
Price/EMA(200)
Price to Exponential moving average (N=200) ratio -1, displayed in percents.
7679
string
Price/EMA(100)
Price to Exponential moving average (N=100) ratio -1, displayed in percents.
7724
string
Price/EMA(50)
Price to Exponential moving average (N=50) ratio -1, displayed in percents.
7681
string
Price/EMA(20)
Price to Exponential moving average (N=20) ratio -1, displayed in percents.
7682
string
Change Since Open
The difference between the last price and the open price.
7683
string
Upcoming Event
Shows the next major company event. Requires Wall Street Horizon subscription.
7684
string
Upcoming Event Date
The date of the next major company event. Requires Wall Street Horizon subscription.
7685
string
Upcoming Analyst Meeting
The date and time of the next scheduled analyst meeting. Requires Wall Street Horizon subscription.
7686
string
Upcoming Earnings
The date and time of the next scheduled earnings/earnings call event. Requires Wall Street Horizon subscription.
7687
string
Upcoming Misc Event
The date and time of the next shareholder meeting, presentation or other event. Requires Wall Street Horizon subscription.
7688
string
Recent Analyst Meeting
The date and time of the most recent analyst meeting. Requires Wall Street Horizon subscription.
7689
string
Recent Earnings
The date and time of the most recent earnings/earning call event. Requires Wall Street Horizon subscription.
7690
string
Recent Misc Event
The date and time of the most recent shareholder meeting, presentation or other event. Requires Wall Street Horizon subscription.
7694
string
Probability of Max Return
Customer implied probability of maximum potential gain.
7695
string
Break Even
Break even points
7696
string
SPX Delta
Beta Weighted Delta is calculated using the formula; Delta x dollar adjusted beta, where adjusted beta is adjusted by the ratio of the close price.
7697
string
Futures Open Interest
Total number of outstanding futures contracts
7698
string
Last Yield
Implied yield of the bond if it is purchased at the current last price. Last yield is calculated using the Last price on all possible call dates. It is assumed that prepayment occurs if the bond has call or put provisions and the issuer can offer a lower coupon rate based on current market rates. The yield to worst will be the lowest of the yield to maturity or yield to call (if the bond has prepayment provisions). Yield to worse may be the same as yield to maturity but never higher.
7699
string
Bid Yield
Implied yield of the bond if it is purchased at the current bid price. Bid yield is calculated using the Ask on all possible call dates. It is assumed that prepayment occurs if the bond has call or put provisions and the issuer can offer a lower coupon rate based on current market rates. The yield to worst will be the lowest of the yield to maturity or yield to call (if the bond has prepayment provisions). Yield to worse may be the same as yield to maturity but never higher.
7700
string
Probability of Max Return
Customer implied probability of maximum potential gain.
7702
string
Probability of Max Loss
Customer implied probability of maximum potential loss.
7703
string
Profit Probability
Customer implied probability of any gain.
7704
string
Organization Type
7705
string
Debt Class
7706
string
Ratings
Ratings issued for bond contract.
7707
string
Bond State Code
7708
string
Bond Type
7714
string
Last Trading Date
7715
string
Issue Date
7720
string
Ask Yield
Implied yield of the bond if it is purchased at the current offer. Ask yield is calculated using the Bid on all possible call dates. It is assumed that prepayment occurs if the bond has call or put provisions and the issuer can offer a lower coupon rate based on current market rates. The yield to worst will be the lowest of the yield to maturity or yield to call (if the bond has prepayment provisions). Yield to worse may be the same as yield to maturity but never higher.
7741
string
Prior Close
Yesterday’s closing price
7762
string
Volume Long
High precision volume for the day. For formatted volume refer to field 87.
7768
string
hasTradingPermissions
if user has trading permissions for specified contract. Returns 1(true) or 0(false).
7920
string
Daily PnL Raw
Your profit or loss of the day since prior close. Daily PnL is calculated with real-time market data (even when not subscribed to market data).
7921
string
Cost Basis Raw
Your current position in this security multiplied by the average price and and multiplier.
Unavailable Historical Data
Copy Location
Historical data is filtered by default. See our FAQ for more insight,
here
.
Bars whose size is 30 seconds or less older than six months
Expired futures data older than two years counting from the future’s expiration date.
Expired options, FOPs, warrants and structured products.
End of Day (EOD) data for options, FOPs, warrants and structured products.
Data for expired future spreads
Data for securities which are no longer trading.
Native historical data for combos. Historical data is not stored in the IB database separately for combos.; combo historical data in TWS or the API is the sum of data from the legs.
Historical data for securities which move to a new exchange will often not be available prior to the time of the move.
Studies and indicators such as Weighted Moving Averages or Bollinger Bands are not available from the API.
Historical Market Data
Copy Location
Get historical market Data for given conid, length of data is controlled by ‘period’ and ‘bar’.
Note
:
There’s a limit of 5 concurrent requests. Excessive requests will return a ‘Too many requests’ status 429 response.
This endpoint provides a maximum of 1000 data points.
GET /iserver/marketdata/history
Request Object
Query Params
conid:
String.
Required
Contract identifier for the ticker symbol of interest.
exchange:
String.
Returns the exchange you want to receive data from.
period:
String.
Overall duration for which data should be returned.
Default to 1w
Available time period– {1-30}min, {1-8}h, {1-1000}d, {1-792}w, {1-182}m, {1-15}y
bar:
String.
Required
Individual bars of data to be returned.
Possible value– 1min, 2min, 3min, 5min, 10min, 15min, 30min, 1h, 2h, 3h, 4h, 8h, 1d, 1w, 1m.
Formatted as: min=minute, h=hour, d=day, w=week, m=month, y=year
See
Step Size
below to ensure your Bar Size is supported for your chosen Period value.
startTime:
String
Starting date of the request duration.
outsideRth:
bool.
Determine if you want data after regular trading hours.
source:
String.
Type of data to be returned.
Possible value– Trades, Midpoint, Bid_Ask
Trades is passed by default.
Python
cURL
request_url = f"{baseUrl}/iserver/marketdata/history?conid=265598&exchange=SMART&period=1d&bar=1d&startTime=20230821-13:30:00&outsideRth=true&source=Midpoint"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/iserver/marketdata/history?conid=265598&exchange=SMART&period=1d&bar=1h&startTime=20230821-13:30:00&outsideRth=true \ 
--request GET
Step Size
A step size is the permitted minimum and maximum bar size for any given period.
period
1min
1h
1d
1w
1m
3m
6m
1y
2y
3y
15y
bar
1min
1min – 8h
1min – 8h
10min – 1w
1h – 1m
2h – 1m
4h – 1m
8h – 1m
1d – 1m
1d – 1m
1w – 1m
default bar
1min
1min
1min
15min
30min
1d
1d
1d
1d
1w
1w
Response Object
serverId:
String.
Internal request identifier.
symbol:
String.
Returns the ticker symbol of the contract.
text:
String.
Returns the long name of the ticker symbol.
priceFactor:
String.
Returns the price increment obtained from the display rules.
startTime:
String.
Returns the initial time of the historical data request.
Returned in UTC formatted as YYYYMMDD-HH:mm:ss
high:
String.
Returns the High values during this time series with format %h/%v/%t.
%h is the high price (scaled by priceFactor),
%v is volume (volume factor will always be 100 (reported volume = actual volume/100))
%t is minutes from start time of the chart
low:
String.
Returns the low value during this time series with format %l/%v/%t.
%l is the low price (scaled by priceFactor),
%v is volume (volume factor will always be 100 (reported volume = actual volume/100))
%t is minutes from start time of the chart
timePeriod:
String.
Returns the duration for the historical data request
barLength:
int.
Returns the number of seconds in a bar.
mdAvailability:
String.
Returns the Market Data Availability.
See the Market Data Availability section for more details.
mktDataDelay:
int.
Returns the amount of delay, in milliseconds, to process the historical data request.
outsideRth:
bool.
Defines if the market data returned was inside regular trading hours or not.
volumeFactor:
int.
Returns the factor the volume is multiplied by.
priceDisplayRule:
int.
Presents the price display rule used.
For internal use only.
priceDisplayValue:
String.
Presents the price display rule used.
For internal use only.
negativeCapable:
bool.
Returns whether or not the data can return negative values.
messageVersion:
int.
Internal use only.
data:
Array of objects.
Returns all historical bars for the requested period.
[{
o:
float.
Returns the Open value of the bar.
c:
float.
Returns the Close value of the bar.
h:
float.
Returns the High value of the bar.
l:
float.
Returns the Low value of the bar.
v:
float.
Returns the Volume of the bar.
t
: int.
Returns the Operator Timezone Epoch Unix Timestamp of the bar.
}],
points:
int.
Returns the total number of data points in the bar.
travelTime:
int.
Returns the amount of time to return the details.
{
  "serverId": "20477",
  "symbol": "AAPL",
  "text": "APPLE INC",
  "priceFactor": 100,
  "startTime": "20230818-08:00:00",
  "high": "17510/472117.45/0",
  "low": "17170/472117.45/0",
  "timePeriod": "1d",
  "barLength": 86400,
  "mdAvailability": "S",
  "mktDataDelay": 0,
  "outsideRth": true,
  "tradingDayDuration": 1440,
  "volumeFactor": 1,
  "priceDisplayRule": 1,
  "priceDisplayValue": "2",
  "chartPanStartTime": "20230821-13:30:00",
  "direction": -1,
  "negativeCapable": false,
  "messageVersion": 2,
  "data": [
    {
      "o": 173.4,
      "c": 174.7,
      "h": 175.1,
      "l": 171.7,
      "v": 472117.45,
      "t": 16923456000
    }
  ],
  "points": 0,
  "travelTime": 48
}
500 System Error
error:
String.
{
  'error': 'description'
}
429 Too many requests
error:
String.
{
  'error': 'description'
}
HMDS Period & Bar Size
Copy Location
Valid Period Units:
Unit
Description
S
Seconds
d
Day
w
Week
m
Month
y
Year
Note: These units are case sensitive.
Valid Bar Units:
Duration
Bar units allowed
Bar size Interval (Min/Max)
60 S
secs | mins
1 secs -> 1mins
3600 S (1hour)
secs | mins | hrs
5 secs -> 1 hours
14400 S (4hours)
sec | mins | hrs
10 secs -> 4 hrs
28800 S  (8hours)
sec | mins | hrs
30 secs -> 8 hrs
1 d
mins | hrs   | d
1 mins-> 1 day
1 w
mins | hrs | d | w
3 mins -> 1 week
1 m
mins | d | w
30 mins -> 1 month
1 y
d | w | m
1 d -> 1 m
Note: These units are case sensitive.
NOTE
: Keep in mind that a step size is defined as the ratio between the historical data request’s duration period and its granularity (i.e. bar size). Historical Data requests need to be assembled in such a way that only a few thousand bars are returned at a time.
Unsubscribe (Single)
Copy Location
Cancel market data for given conid.
POST /iserver/marketdata/unsubscribe
Request Object
Body Params
conid:
String.
Required
Enter the contract identifier to cancel the market data feed.
This can clear all standing market data feeds to invalidate your cache and start fresh.
Python
cURL
request_url = f"{baseUrl}/iserver/marketdata/unsubscribe" 
json_content ={
  "conid":265598 
}
requests.post(url=request_url, json=json_content)
curl \
--url {{baseUrl}}/iserver/marketdata/unsubscribe \ 
--request POST
--data '{
  "conid":265598
}'
Response Object
success:
bool.
Returns a confirmation status of your unsubscribe request. A true response indicates that the market data feed has been successfully cancelled.
{
  "success": true
}
Eror Response Object
A status 500 response will be sent when attempting to unsubscribe from a market data feed that is not currently open.
error:
String.
Returns an error response with message unknown indicating that the user does not have an existing data feed for the given conid.
{
  "error": "unknown"
}
Unsubscribe (All)
Copy Location
Cancel all market data request(s). To cancel market data for a specific conid, see /iserver/marketdata/{conid}/unsubscribe.
GET /iserver/marketdata/unsubscribeall
Request Object
No params or arguments should be passed.
Python
cURL
request_url = f"{baseUrl}/iserver/marketdata/unsubscribeall"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/iserver/marketdata/unsubscribeall \ 
--request GET
Response Object
confirmed:
String.
Returns a confirmation status of your unsubscribe request.
{
  "unsubscribed": true
}
Option Chains
Copy Location
Option Chains are not through a singular request. However, they can be retrieved through a specific process in the Client Portal API. The steps below the standard procedure to retrieve a range of option conIds that can be filtered into market data.
Step One: Instantiate the Option Chain
Copy Location
To begin, users must first make a call to the
/iserver/secdef/search endpoint
for the underlying symbol. This is required for all future steps every time the user does not know the final derivative’s conId.
This must always be called before proceeding, even if you are aware of the conId and expiration dates.
Python
cURL
request_url = f"{baseUrl}/iserver/secdef/search?symbol=SPX"
requests.get(url=request_url)
curl --insecure \
--url https://localhost:5000/v1/api/iserver/secdef/search?symbol=SPX \
--request GET
In the response, we are able to see two important values returned. The first, we can find our ConID for the underlying, 416904. We will need this for our future requests.
We can also see under “sections”::”secType”:”OPT, “months” we will see all of the contract expirations months. This will be required to build our option chain in the next request.
[
  {
    "conid": "416904",
    "companyHeader": "S&P 500 Stock Index - CBOE",
    "companyName": "S&P 500 Stock Index",
    "symbol": "SPX",
    "description": "CBOE",
    "restricted": "IND",
    "sections": [
      {...},
      {
        "secType": "OPT",
        "months": "JAN24;FEB24;MAR24;APR24;MAY24;JUN24;JUL24;AUG24;SEP24;OCT24;NOV24;DEC24;JAN25;MAR25;JUN25;DEC25;DEC26;DEC27;DEC28;DEC29",
        "exchange": "SMART;CBOE;IBUSOPT"
      },
      {...}
    ]
  }
]
Step Two: Find Potential Strikes
Copy Location
After querying the /iserver/secdef/search endpoint, developers should now call the
/iserver/secdef/strikes endpoint
. To receive the appropriate strikes, the conId, secType, and expiration month should be specified.
This must always be called before proceeding, even if you are aware of the strikes.
Notes:
For Futures Options, the conId of the Index should be specified, along with the explicit exchange being listed. As an example, CL futures options should specify “exchange=NYMEX” as an additional query parameter.
The inclusion of the name field will prohibit the /iserver/secdef/strikes endpoint from returning data. After retrieving your expected contract, customers looking to create option chains should remove the name field from the request.
Python
cURL
request_url = f"{baseUrl}/iserver/secdef/strikes?conid=416904&sectype=OPT&month=JAN25"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/iserver/secdef/strikes?conid=416904&secType=OPT&month=JAN25 \
--request GET
As a response, an object containing arrays of all Call and Put strikes will be returned. This will only return potential strike prices. This does not necessarily indicate. These strikes should be confirmed with our /info endpoint to confirm if the strike is valid.
Note:
This endpoint will always return empty arrays unless
/iserver/secdef/search
is called for the same underlying symbol beforehand. The inclusion of the name field with the
/iserver/secdef/search
endpoint will prohibit the strikes endpoint from returning data. After retrieving your expected contract from the initial search, developers looking to create option chains should remove the name field from the request.
{
  "call": [
    200.0,
  {...},
    7800.0
  ],
  "put": [
    200.0,
  {...},
    7800.0
  ]
}
Step Three: Validate The Contract
Copy Location
After calling the /search and /strikes endpoints, users can use the
/iserver/secdef/info endpoint
to validate the derivative conId. This endpoint should be called for each strike and right combination of interest.
Note: For Futures Options, the conId of the Index should be specified, along with the explicit exchange being listed. As an example, CL futures options should specify “exchange=NYMEX” as an additional query parameter.
Python
cURL
request_url = f"{baseUrl}/iserver/secdef/info?conid=416904&secType=OPT&month=JAN25&strike=3975&right=P
requests.get(url=request_url)
curl \
--url {{baseUrl}}/iserver/secdef/info?conid=416904&secType=OPT&month=JAN25&strike=3975&right=P \
--request GET
While all of the information is relevant, it is most important to save the conId in order to track the contract itself. For the lifespan of the Option, this conId will remain constant. This will also be used for all subsequent requests for market data or order placement.
[
  {
    "conid": 654371995,
    "symbol": "SPX",
    "secType": "OPT",
    "exchange": "SMART",
    "listingExchange": null,
    "right": "P",
    "strike": 3975.0,
    "currency": "USD",
    "cusip": null,
    "coupon": "No Coupon",
    "desc1": "SPX",
    "desc2": "JAN 16 '25 3975 Put (AM)",
    "maturityDate": "20250116",
    "multiplier": "100",
    "tradingClass": "SPX",
    "validExchanges": "SMART,CBOE,IBUSOPT"
  }
]
Final Steps
Copy Location
After confirming all of our interested strikes to retrieve our conIds, we officially have our option chain established. From there, users may be interested to send requests to the
/iserver/marketdata/snapshot endpoint
in bulk by comma separating the “conids” list.
Alternatively, users that already are aware of the market data potentially through other means can look to start placing orders using the
/iserver/account/{accountId}/orders endpoint
.
Order Monitoring
Copy Location
Live Orders
Copy Location
This endpoint requires a pre-flight request.
Orders is the list of live orders (cancelled, filled, submitted).
To retrieve order information for a specific account, clients must first query the
/iserver/account endpoint
to switch to the appropriate account.
Please be aware that filtering orders using the /iserver/account/orders endpoint will prevent order details from coming through over the
websocket “sor” topic
. To resolve this issue, developers should set “force=true” in a follow-up /iserver/account/orders call to clear any cached behavior surrounding the endpoint prior to calling for the websocket request.
GET /iserver/account/orders
Request Object
Query Params
filters:
String.
Optionally filter your list of orders by a unique status value. More than one filter can be passed, separated by commas.
force:
bool.
Force the system to clear saved information and make a fresh request for orders. Submission will appear as a blank array.
Python
cURL
request_url = f"{baseUrl}/iserver/account/orders?filters=filled&force=true"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/iserver/account/orders?filters=filled&force=true \
--request GET
Response Object
NOTE:
: The /iserver/account/orders endpoint can contain a maximum of 1000 orders.
orders:
Array of objects.
Contains all orders placed on the account for the day.
[{
acct:
String.
Returns the accountID for the submitted order.
conidex:
String.
Returns the contract identifier for the order.
conid:
int.
Returns the contract identifier for the order.
orderId:
int.
Returns the local order identifier of the order.
cashCcy:
String.
Returns the currency used for the order.
sizeAndFills:
String.
Returns the size of the order and how much of it has been filled.
orderDesc:
String.
Returns the description of the order including the side, size, order type, price, and tif.
description1:
String.
Returns the local symbol of the order.
ticker:
String.
Returns the ticker symbol for the order.
secType:
String.
Returns the security type for the order.
listingExchange:
String.
Returns the primary listing exchange of the orer.
remainingQuantity:
float.
Returns the remaining size for the order to fill.
filledQuantity:
float.
Returns the size of the order already filled.
companyName:
String.
Returns the company long name.
status:
String.
Returns the current status of the order.
order_ccp_status:
String.
Returns the current status of the order.
origOrderType:
String.
Returns the original order type of the order, whether or not the type has been changed.
supportsTaxOpt:
String.
Returns if the order is supported by the Tax Optimizer.
lastExecutionTime:
String.
Returns the datetime of the order’s most recent execution.
Time returned is based on UTC timezone.
Value Format: YYMMDDHHmmss
orderType:
String.
Returns the current order type, or the order at the time of execution.
bgColor:
String.
Internal use only.
fgColor:
String.
Internal use only.
order_ref:
String.
User defined string used to identify the order. Value is set using “cOID” field while placing an order.
timeInForce:
String.
Returns the time in force (tif) of the order.
lastExecutionTime_r:
int.
Returns the epoch time of the most recent execution on the order.
side:
String.
Returns the side of the order.
avgPrice:
String.
Returns the average price of execution for the order.
}]
snapshot:
bool.
Returns if the data is a snapshot of the account’s orders.
{
  "orders": [
    {
      "acct": "U1234567",
      "conidex": "265598",
      "conid": 265598,
      "account": "U1234567",
      "orderId": 1234568790,
      "cashCcy": "USD",
      "sizeAndFills": "5",
      "orderDesc": "Sold 5 Market, GTC",
      "description1": "AAPL",
      "ticker": "AAPL",
      "secType": "STK",
      "listingExchange": "NASDAQ.NMS",
      "remainingQuantity": 0.0,
      "filledQuantity": 5.0,
      "totalSize": 5.0,
      "companyName": "APPLE INC",
      "status": "Filled",
      "order_ccp_status": "Filled",
      "avgPrice": "192.26",
      "origOrderType": "MARKET",
      "supportsTaxOpt": "1",
      "lastExecutionTime": "231211180049",
      "orderType": "Market",
      "bgColor": "#FFFFFF",
      "fgColor": "#000000",
      "order_ref": "Order123",
      "timeInForce": "GTC",
      "lastExecutionTime_r": 1702317649000,
      "side": "SELL"
    }
  ],
  "snapshot": true
}
Order Status
Copy Location
The Order Status endpoint may be used to monitor a single specific order while it remains active.
Important Notes:
For multi-account structures such as Financial Advisors or linked-account structures, users must call
/iserver/account
to switch to the affiliated account before requesting order status. It is otherwise expected to result in a ‘503’ error.
If an order has been cancelled or filled prior to the active session and there is no cached information saved, querying the order status endpoint would be expected to result in a ‘503’ error.
Retrieve the given status of an individual order using the orderId returned by the order placement response or the orderId available in the live order response.
GET /iserver/account/order/status/{{ orderId }}
Request Object
Query Params
orderId:
String.
Required
Order identifier for the placed order. Returned by the order placement response or the orderId available in the live order response.
Python
cURL
request_url = f"{baseUrl}/iserver/account/order/status/1234567890"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/iserver/account/order/status/1234567890\
--request GET
Response Object
sub_type:
null.
Internal use only.
request_id:
String.
Returns the requestId of the order palced by the user.
order_id:
int.
Returns the orderId of the requested order.
conidex:
String.
Returns the contract identifier for the order.
conid:
int.
Returns the contract identifier for the order.
symbol:
String.
Returns the ticker symbol for the order.
side:
String.
Returns the side of the order.
contract_description_1:
String.
Returns the local symbol of the order.
listing_exchange:
String.
Returns the primary listing exchange of the orer.
option_acct:
String.
For Client Portal use (Internal use only).
company_name:
String.
Returns the company long name.
size:
String.
Remainder of order to be filled
total_size:
String.
Total quantity of the order.
currency:
String.
Returns the base currency of the order.
account:
String.
Returns the account the order was placed for.
order_type:
String.
Returns the order type for the given order.
cum_fill:
String.
Total filled quantity of the order
order_status:
String.
Returns the current status of the order.
order_ccp_status:
String.
Returns the current status of the order as a code.
order_status_description:
String.
Returns the human readable response of the order status.
tif:
String.
Returns the time in force of the order.
fg_color:
String.
For Client Portal use (Internal use only).
bg_color:
String.
For Client Portal use (Internal use only).
order_not_editable:
bool.
Returns whether or not the order can be modified.
This is relevant for orders that are currently or have already been executed.
editable_fields:
null.
For Client Portal use (Internal use only).
cannot_cancel_order:
bool.
Returns whether or not the order can be cancelled.
This is relevant for orders that are currently or have already been executed.
deactivate_order:
bool.
Return whether or not the order has been marked inactive.
sec_type:
String.
Returns the security type of the order’s contract.
available_chart_periods:
String.
For Client Portal use (Internal use only).
order_description:
String.
Returns the description of the order including the side, size, order type, price, and tif.
order_description_with_contract:
String.
Returns the description of the order including the side, size, symbol, order type, price, and tif.
alert_active:
int.
Returns wheteher or not there is an active alert available on the order.
child_order_type:
String.
type of the child order
Value Format: A=attached, B=beta-hedge, 0=No Child
order_clearing_account:
String.
Returns the accountID for the submitted order.
size_and_fills:
String.
Displays
size / cumulative fill
exit_strategy_display_price:
String.
Displays the price of the order as it resolved its execution.
exit_strategy_chart_description:
String.
Returns the description of the order including the side, size, order type, price, and tif.
average_price:
String.
Returns the average price of execution for the order.
exit_strategy_tool_availability:
String.
Internal use only.
allowed_duplicate_opposite:
bool.
Returns whether or not the opposing order can be placed on the market.
order_time:
String.
Returns the datetime of the order placement.
Time returned is based on UTC timezone.
Value Format: YYMMDDHHmmss
{
  "sub_type": null,
  "request_id": "209",
  "server_id": "0",
  "order_id": 1799796559,
  "conidex": "265598",
  "conid": 265598,
  "symbol": "AAPL",
  "side": "S",
  "contract_description_1": "AAPL",
  "listing_exchange": "NASDAQ.NMS",
  "option_acct": "c",
  "company_name": "APPLE INC",
  "size": "0.0",
  "total_size": "5.0",
  "currency": "USD",
  "account": "U1234567",
  "order_type": "MARKET",
  "cum_fill": "5.0",
  "order_status": "Filled",
  "order_ccp_status": "2",
  "order_status_description": "Order Filled",
  "tif": "DAY",
  "fg_color": "#FFFFFF",
  "bg_color": "#000000",
  "order_not_editable": true,
  "editable_fields":"",
  "cannot_cancel_order": true,
  "deactivate_order": false,
  "sec_type": "STK",
  "available_chart_periods": "#R|1",
  "order_description": "Sold 5 Market, Day",
  "order_description_with_contract": "Sold 5 AAPL Market, Day",
  "alert_active": 1,
  "child_order_type": "0",
  "order_clearing_account": "U1234567",
  "size_and_fills": "5",
  "exit_strategy_display_price": "193.12",
  "exit_strategy_chart_description": "Sold 5 @ 192.26",
  "average_price": "192.26",
  "exit_strategy_tool_availability": "1",
  "allowed_duplicate_opposite": true,
  "order_time": "231211180049"
}
Order Status Value
Copy Location
For many orders, customers will see orders return an order status with an array of potential values. The table below elaborates on what the status means for the order and the potential behavior to expect from it.
The values returned from the “order_status” field of our Live Orders object will vary slightly from the format used while using the “filters” parameter from
GET /iserver/account/orders
.
Status
Filter Value
Description
Inactive
inactive
Indicates that you are in the process of creating an order and you have not yet activated or transmitted it.
PendingSubmit
pending_submit
Indicates that you have transmitted your order, but have not yet received confirmation that it has been accepted by the order destination.
PreSubmitted
pre_submitted
Indicates that an order has been accepted by the system (simulated orders) or an exchange (native orders) and that this order has yet to be elected.
Submitted
submitted
Indicates that your order has been accepted and is working at the destination.
Filled
filled
Order has been completely filled.
PendingCancel
pending_cancel
Indicates that you have sent a request to cancel the order but have not yet received cancel confirmation from the order destination.  At this point, your order is not confirmed canceled. You may still receive an execution while your cancellation request is pending.
PreCancelled
pre_cancelled
Indicates that a cancellation request has been accepted by the system but that currently the request is not being recognized, due to system, exchange or other issues. At this point, your order is not confirmed canceled. You may still receive an execution while your cancellation request is pending.
Cancelled
cancelled
Indicates that the balance of your order has been confirmed canceled by the system. This could occur unexpectedly when the destination has rejected your order.
WarnState
warn_state
Order has a specific warning message such as for basket orders.
N/A
sort_by_time
There is an initial sort by order state performed so active orders are always above inactive and filled then orders are sorted chronologically.
Trades
Copy Location
Returns a list of trades for the currently selected account for current day and six previous days. It is advised to call this endpoint once per session.
GET /iserver/account/trades
Request Object
Query Params
days:
String.
Specify the number of days to receive executions for, up to a maximum of 7 days.
If unspecified, only the current day is returned.
Python
cURL
request_url = f"{baseUrl}/iserver/account/trades?days=3"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/iserver/account/trades?days=3 \
--request GET
Response Object.
execution_id:
String.
Returns the execution ID for the trade.
symbol:
String.
Returns the underlying symbol.
supports_tax_opt:
String.
Returns whether or not tax optimizer is supported for the order.
side:
String.
Returns the side of the order, Buy or Sell.
order_description:
String.
Returns the description of the order including the side, size, symbol, order type, price, and tif.
order_ref:
String.
User defined string used to identify the order. Value is set using “cOID” field while placing an order.
trade_time:
String.
Returns the UTC format of the trade time.
trade_time_r:
int.
Returns the epoch time of the trade.
size:
float.
Returns the quantity of the order.
price:
String.
Returns the price of trade execution.
submitter:
String.
Returns the username that submitted the order.
exchange:
String.
Returns the exchange the order was executed on.
commission:
String.
Returns the cost of commission for the trade.
net_amount:
float.
Returns the total net cost of the order.
account:
String.
Returns the account identifier.
accountCode:
String.
Returns the account identifier.
company_name:
String.
Returns the long name of the contract’s company.
contract_description_1:
String.
Returns the local symbol of the order.
sec_type:
String.
Returns the security type of the contract.
listing_exchange:
String.
Returns the primary listing exchange of the contract.
conid:
int.
Returns the contract identifier of the order.
conidEx:
String.
Returns the contract identifier of the order.
clearing_id:
String.
Returns the clearing firm identifier.
clearing_name:
String.
Returns the clearing firm identifier.
liquidation_trade:
String.
Returns whether the order was part of an account liquidation or note.
is_event_trading:
String.
Returns whether the order was part of event trading or not.
[
  {
    "execution_id": "0000e0d5.6576fd38.01.01",
    "symbol": "AAPL",
    "supports_tax_opt": "1",
    "side": "S",
    "order_description": "Sold 5 @ 192.26 on ISLAND",
    "trade_time": "20231211-18:00:49",
    "trade_time_r": 1702317649000,
    "size": 5.0,
    "price": "192.26",
    "order_ref": "Order123",
    "submitter": "user1234",
    "exchange": "ISLAND",
    "commission": "1.01",
    "net_amount": 961.3,
    "account": "U1234567",
    "accountCode": "U1234567",
    "account_allocation_name": "U1234567",
    "company_name": "APPLE INC",
    "contract_description_1": "AAPL",
    "sec_type": "STK",
    "listing_exchange": "NASDAQ.NMS",
    "conid": 265598,
    "conidEx": "265598",
    "clearing_id": "IB",
    "clearing_name": "IB",
    "liquidation_trade": "0",
    "is_event_trading": "0"
  }
]
Orders
Copy Location
Place Order
Copy Location
When connected to an IServer Brokerage Session, this endpoint will allow you to submit orders.
CP WEB API supports various advanced orderTypes, for additional details and examples refer to the
Order Types
page.
Cash Quantity:
Send orders using monetary value by specifying cashQty instead of quantity, e.g. cashQty: 200. The endpoint /iserver/contract/rules returns list of valid orderTypes in cqtTypes.
Note: See
Cash Quantity Orders in the Web API
for more details.
Currency Conversion:
Convert cash from one currency to another by including isCcyConv = true. To specify the cash quantity use fxQTY instead of quantity, e.g. fxQTY: 100.
IB Algos:
Attached user-defined settings to your trades by using any of IBKR’s Algo Orders. Use the endpoint /iserver/contract/{conid}/algos to identify the available strategies for a contract.
Notes:
With the exception of OCA groups and bracket orders, the orders endpoint does not currently support the placement of unrelated orders in bulk.
Developers should not attempt to place another order until the previous order has been fully acknowledged, that is, when no further warnings are received deferring the client to the reply endpoint.
POST /iserver/account/{accountID}/orders
Request Object
Path Params
accountId:
String.
The account ID for which account should place the order.
Financial Advisors should instead specify their allocation group.
Body Params
orders:
Array of Objects.
Required
Used to the order content.
[{
acctId:
String.
It should be one of the accounts returned by /iserver/accounts.
If not passed, the first one in the list is selected.
conid:
int.
Required*
conid is the identifier of the security you want to trade.
Using the conid field will force the order to be SMART-routed, even if conidex is specified.
You can find the conid with /iserver/secdef/search.
*Can use conidex instead of conid.
conidex:
int.
Required*
conidex is the identifier for the security and exchange you want to trade.
Direct routed orders cannot use the conid field in addition to conidex, otherwise the order will be automatically routed to SMART.
You can find the conid and list of exchanges with /iserver/secdef/search.
*Can use conidex instead of conid.
manualIndicator:
boolean.
Required*
IMPORTANT
This field is required when trading Futures and Futures Options contracts to remain in compliance with
CME Group Rule 536-B
.
The Manual Order Indicator is used to determine if an order was manual entered or done through an automated tool.
true indicates the order was submitted manually through an interface while false indicates an order was submitted autonomously.
extOperator:
string.
Required*
IMPORTANT
This field is required when trading Futures and Futures Options contracts to remain in compliance with
CME Group Rule 536-B
.
The External Operator field should contain information regarding the submitting user in charge of the API operation at the time of request submission.
secType:
String.
The contract-identifier (conid) and security type (type) specified as a concatenated value
Value Format: “conid:type”
cOID:
String.
Customer Order ID.
An arbitrary string that can be used to identify the order
The value must be unique for a 24h span.
The value can be
no longer than 64 characters
.
Do not set this value for child orders when placing a bracket order.
parentId:
String.
Only specify for child orders when placing bracket orders.
The parentId for the child order(s) must be equal to the cOId (customer order id) of the parent.
orderType:
String.
Required
The order-type determines what type of order you want to send.
Available Order Types: LMT, MKT, STP, STOP_LIMIT, MIDPRICE, TRAIL, TRAILLMT
listingExchange:
String.
Primary routing exchange for the order.
By default we use “SMART” routing.
Possible values are available via the endpoint: /iserver/contract/{conid}/info
isSingleGroup:
bool.
Set to true if you want to place a single group orders(OCA)
outsideRTH:
bool.
Set to true if the order can be executed outside regular trading hours.
price:
float.
Required for LMT or STOP_LIMIT
This is typically the limit price.
For STP|TRAIL this is the stop price.
For MIDPRICE this is the option price cap.
auxPrice:
float.
Required for STOP_LIMIT and TRAILLMT orders.
Stop price for STOP_LIMIT and TRAILLMT orders.
You must specify both price and auxPrice for STOP_LIMIT|TRAILLMT orders.
side:
String.
Required
Valid Values: SELL or BUY
ticker:
String.
This is the underlying symbol for the contract.
tif:
String.
Required
The Time-In-Force determines how long the order remains active on the market.
Valid Values: GTC, OPG, DAY, IOC, PAX (CRYPTO ONLY).
trailingAmt:
float.
Required for TRAIL and TRAILLMT order
optional if order is TRAIL, or TRAILLMT.
When trailingType is amt, this is the trailing amount
When trailingType is %, it means percentage.
trailingType:
String.
Required for TRAIL and TRAILLMT order
This is the trailing type for trailing amount.
You must specify both trailingType and trailingAmt for TRAIL and TRAILLMT order
Valid Values: “amt” or “%”
allOrNone:
Boolean. Determine if the order should be executed in it entirety at once (True), or if the order may be filled through multiple executions (False)
customerAccount:
String. Required for Nondisclosed Omnibus Accounts
A unique identifier for each account within the Omnibus structure to signify the account holder being traded.
Best practice (Not Required): clients should look to hash this value, using something along the lines of 5 digits of SHA1 of the account number.
This should not be implemented for non-omnibus accounts.
isProCustomer:
Boolean. Required for Nondisclosed Omnibus Accounts
Signify whether or not the subaccount is classified as Professional or Non-Professional.
This should not be implemented for non-omnibus accounts.
referrer:
String.
Custom order reference
quantity:
float.
Required*
Used to designate the total number of shares traded for the order.
Only whole share values are supported.
cashQty:
float.
Used to specify the monetary value of an order instead of the number of shares.
When using ‘cashQty’ don’t specify ‘quantity’
Cash quantity orders are provided on a non-guaranteed basis.
Cash Quantity orders are only supported for Cryptocurrency and Forex contracts.
fxQty:
float.
This is the cash quantity field which can only be used for Currency Conversion Orders.
When using ‘fxQty’ don’t specify ‘quantity’.
useAdaptive:
boolean
If true, the system will use the Price Management Algo to submit the order.
Read more on our Price Management Algo page. https://www.interactivebrokers.com/en/index.php?f=43423
isCcyConv:
boolean
set to true if the order is a FX conversion order
allocationMethod:
String.
Set the allocation method when placing an order using an FA account for a group.
Based on value set in Trader Workstation.
manualOrderTime:
int.
Only used for Brokers and Advisors. Mark the time to manually record initial order entry.
Must be sent as epoch time integer.
deactivated:
bool.
Functions the same as
Saving an Order in Trader Workstation
.
strategy:
String.
Specify which IB Algo algorithm to use for this order.
strategyParameters:
Array.
The IB Algo parameters for the specified algorithm.
}]
Python
cURL
request_url = f"{baseUrl}/iserver/account/U1234567/orders"
json_content = {
  "orders": [
    {
      "acctId": "U1234567",
      "conid": 265598,
      "conidex": "265598@SMART",
      "manualIndicator": True,
      "extOperator":"person1234",
      "secType": "265598@STK",
      "cOID": "AAPL-BUY-100",
      "parentId": None,
      "orderType": "TRAILLMT",
      "listingExchange": "NASDAQ",
      "isSingleGroup": false,
      "outsideRTH": true,
      "price": 185.50,
      "auxPrice": 183,
      "side": "BUY",
      "ticker": "AAPL",
      "tif": "GTC",
      "trailingAmt": 1.00,
      "trailingType": "amt",
      "referrer": "QuickTrade",
      "quantity": 100,
      # Can not be used in tandem with quantity value.
      # "cashQty": {{ cashQty }},
      # "fxQty": {{ fxQty }},
      "useAdaptive": false,
      "isCcyConv": false,
      # May specify an allocation method such as Equal or NetLiq for Financial Advisors.
      # "allocationMethod": {{ allocationMethod }},
      "strategy": "Vwap",
        "strategyParameters": {
          "MaxPctVol":"0.1",
          "StartTime":"14:00:00 EST",
          "EndTime":"15:00:00 EST",
          "AllowPastEndTime":true
        }
    }
  ]
}
requests.post(url=request_url, json=json_content)
curl \
--url {{baseUrl}}/iserver/account/U1234567/orders \
--request POST \
--header 'Content-Type:application/json' \
--data '{
  "orders": [
    {
      "acctId": "U1234567",
      "conid": 265598,
      "conidex": "265598@SMART",
      "manualIndicator": true,
      "extOperator":"person1234",
      "secType": "265598:STK",
      "cOID": "AAPL-BUY-100",
      "parentId": null,
      "orderType": "TRAILLMT",
      "listingExchange": "ISLAND",
      "isSingleGroup": false,
      "outsideRth": true,
      "price": 185.50,
      "auxPrice": 183,
      "side": "BUY",
      "ticker": "AAPL",
      "tif": "GTC",
      "trailingAmt": 1.00,
      "trailingType": "amt",
      "referrer": "QuickTrade",
      "quantity": 100,
      # Can not be used in tandem with quantity value.
      # "cashQty": {{ cashQty }}, 
      # "fxQty": {{ fxQty }},
      "useAdaptive": false,
      "isCcyConv": false,
      # May specify an allocation method such as Equal or NetLiq for Financial Advisors.
      # "allocationMethod": {{ allocationMethod }},
      "strategy": "Vwap",
        "strategyParameters": {
          "MaxPctVol":"0.1",
          "StartTime":"14:00:00 EST",
          "EndTime":"15:00:00 EST",
          "AllowPastEndTime":true
        }
    }
  ]
}'
Response Object
orderId:
String.
Returns the orders identifier which can be used for order tracking, modification, and cancellation.
order_status:
String.
Returns the order status of the current market order.
See
Order Status Value
for more information.
encrypt_message:
String.
Returns a “1” to display that the message sent was encrypted.
[
  {
    "order_id": "1234567890",
    "order_status": "Submitted",
    "encrypt_message": "1"
  }
]
Alternate Response Object
In some instances, you will receive an ID along with a message about your order.
See the
Place Order Reply
section for more details on resolving the confirmation.
Users that wish to avoid receiving /reply message should consider using the
Suppression
endpoint to automatically accept them.
Important:
The reply must be confirmed before sending any further orders. Otherwise, the order will be invalidated and attempts to confirm invalid replies will result in a timeout (503).
id:
String.
Returns a message ID relating to the particular order’s warning confirmation.
message:
Array of Strings.
Returns the message warning about why the order wasn’t initially transmitted.
isSuppressed:
bool.
Returns if a particular warning was suppressed before sending.
Always returns false.
messageIds:
Array of Strings.
Returns an internal message identifier (Internal use only).
[
  {
    "id": "07a13a5a-4a48-44a5-bb25-5ab37b79186c",
    "message": [
      "The following order \"BUY 5 AAPL NASDAQ.NMS @ 150.0\" price exceeds \nthe Percentage constraint of 3%.\nAre you sure you want to submit this order?"
    ],
    "isSuppressed": false,
    "messageIds": [
      "o163"
    ]
  }
]
Order Reject Object
In the event an order is placed that can not be completed based on account details such as trading permissions or funds, customers will receive a 200 OK response along with an error message explaining the issue.
This is unique from the 200 response used in the Alternate Response Object, or a potential 500 error resulting from invalid request content.
{
  "error":"We cannot accept an order at the limit price you selected. Please submit your order using a limit price that is closer to the current market price of 197.79.  Alternatively, you can convert your order to an Algorithmic Order (IBALGO)."
}
Cash Quantity Orders in the Web API
Copy Location
Cash Quantity orders are only supported for Cryptocurrency, Forex, and Stock contracts.
Stock orders submitted using Cash Quantity field through the API will round down to the nearest whole share.
In the event an order is submitted with a value less than one share will result in rejection of the order.
Orders submitted for Crypto or Forex will be traded directly as submitted.
Place Order Reply Confirmation
Copy Location
Confirm order precautions and warnings presented from placing orders. Orders
must
be replied to immediately after receiving the reply message. Submitting other orders or other requests will cancel the order and attempts to acknowledge the reply will result in a 503 error.
Users that wish to avoid receiving /reply message should consider using the
Suppression
endpoint to automatically accept them.
POST /iserver/reply/{{ replyId }}
Request Object
Path Params
replyId:
String.
Required
Include the id value from the prior order request relating to the particular order’s warning confirmation.
Body Params
confirmed:
bool.
Required
Pass your confirmation to the reply to allow or cancel the order to go through.
true will agree to the message transmit the order.
false will decline the message and discard the order.
Python
cURL
request_url = f"{baseUrl}/iserver/reply/a12b34c5-d678-9e012f-3456-7a890b12cd3e"
json_content = {"confirmed":true}
requests.post(url=request_url, json=json_content)
curl \
--url {{baseUrl}}/iserver/reply/a12b34c5-d678-9e012f-3456-7a890b12cd3e \
--request POST \
--header 'Content-Type:application/json' \
--data '{"confirmed":true}'
Response Object
orderId:
String.
Returns the orders identifier which can be used for order tracking, modification, and cancellation.
order_status:
String.
Returns the order status of the current market order.
See
Order Status Value
for more information.
encrypt_message:
String.
Returns a “1” to display that the message sent was encrypted.
[
  {
    "order_id": "1234567890",
    "order_status": "Submitted",
    "encrypt_message": "1"
  }
]
NOTE:
After sending your initial confirmation to the /iserver/reply/{replyId} endpoint, you may receive additional reply messages. These confirmation messages must also be responded to before the order will submit.
Respond to a Server Prompt
Copy Location
Respond to a server prompt received via ntf webscoket message.
POST /iserver/notification
Request Object
Body Params
orderId
int.
Required
IB-assigned order identifier obtained from the ntf websocket message that delivered the server prompt.
reqId
string.
Required
IB-assigned request identifier obtained from the ntf websocket message that delivered the server prompt.
text
string.
Required
The selected value from the “options” array delivered in the server prompt ntf websocket message.
Python
cURL
request_url = f"{baseUrl}/iserver/notification"
json_content = {
  "orderId": 987654321,
  "reqId": "12345",
  "text": "Yes"
}
requests.post(url=request_url, json=json_content)
curl \
--url {baseUrl}/iserver/notification \
--request POST \
--header 'Content-Type:application/json' \
--data '{
  "orderId": 987654321,
  "reqId": "12345",
  "text": "Yes"
}'
Response Object
{Status text}:
string
Returns the status of the confirmation message.
Success
Order Error Details
Copy Location
The WebAPI contains an array of unique error response systems dependent upon the layer a request fails at. The table below describes the status code, status message, and error message that may have lead to the issue.
Web API Errors
Status Code
Status Message
Error Message
Context
400
BadRequest
Failed to parse body as JSON. Content Type: application/json; charset=utf-8
Request body is not properly formatted as JSON.
400
BadRequest
orders request includes parameter with incorrect type
Field requiring a specific variable type is receiving an invalid type. See Place Order reference for support types.
400
BadRequest
Invalid Side (must be ‘BUY’ or ‘SELL’).
Value for Side is invalid.
400
BadRequest
Unknown order type
Provided order type is inaccurate.
400
BadRequest
conid or conidex is required
conid or conidex are not included or misspelled.
Interactive Brokers Errors & Precautions
Status Code
Status Message
Error Message
Context
200
OK
Invalid order price fields
Price value is not supported. Ensure the price adheres to market rules.
200
OK
null time in force is not supported for this order
No tif value was given, or the tif value was invalid.
200
OK
“id”:”{{Reply_ID}}”,”message”:[“{{Message}}”],”isSuppressed”:false,”messageIds”:[“{{Message_ID}}”],”messageOptions”:[“Yes”,”No”]
See
Suppressible Message IDs
for specific ID information.
200
OK
no sec defs returned forSecDef  reqId=resolve ecReqByConid13297
Provided conid or conidex is invalid. Verify contract against /iserver/secdef/info.
Preview Order / WhatIf Order
Copy Location
This endpoint allows you to preview order without actually submitting the order and you can get commission information in the response. Also supports bracket orders.
Note:
Please be aware that /whatif orders are also effected by our
message suppression endpoint
.
Clients must query
/iserver/marketdata/snapshot
for the instrument prior to requesting the /whatif endpoint.
POST /iserver/account/{accountId}/orders/whatif
Request Object
The body content of the /whatif endpoint will follow the same structure as the standard /iserver/account/{accountId}/orders endpoint.
See the
Place Order
section for more details.
Python
cURL
request_url = f"{baseUrl}/iserver/account/U1234567/orders/whatif"
json_content = {
  "orders": [
    {
      "conid": 265598,
      "orderType": "LMT",
      "price": 200.25,
      "side": "BUY",
      "tif": "DAY",
      "quantity": 5
    }
  ]
}
requests.post(url=request_url, json=json_content)
curl \
--url {{baseUrl}}/iserver/account/U1234567/orders/whatif \
--request POST \
--header 'Content-Type:application/json' \
--data '{
  "orders": [
    {
      "conid": 265598,
      "orderType": "LMT",
      "price": 200.25,
      "side": "BUY",
      "tif": "DAY",
      "quantity": 5
    }
  ]
}'
Response Object
amount:
Object.
Contains the details about the order cost.
{
amount:
String.
Returns the cost of the base order.
commission:
String.
Returns the commission cost of the base order.
total:
String.
Returns the total cost of the order.
},
equity:
Object.
Contains the details about the order’s impact on your equity.
{
current:
String.
Returns the current equity of the account.
change:
String.
Returns the equity impact from the order.
after:
String.
Returns the equity after the order is traded.
},
initial:
Object.
Contains the details about the order’s impact on your initial margin.
{
current:
String.
Returns the current initial margin value.
change:
String.
Returns the amount the initial margin will change by.
after:
String.
Returns the initial margin value after the order.
},
maintenance:
Object.
Contains the details about the order’s impact on your maintenance margin.
{
current:
String.
Returns the current maintenance margin value.
change:
String.
Returns the amount the maintenance margin will change by.
after:
String.
Returns the maintenance margin value after the transaction.
},
position:
Object.
Contains the details about the order’s impact on your current position.
{
current:
String.
Returns the cost of the base order.
change:
String.
Returns the cost of the base order.
after:
String.
Returns the cost of the base order.
},
warn:
String.
Returns any potential warning message from placing this order.
Returns null if no warning is possible.
error:
String.
Returns any potential error message from placing this order.
Returns null if no error is possible.
{
  "amount": {
    "amount": "1,977.60 USD (10 Shares)",
    "commission": "1 USD",
    "total": "1,978.60 USD"
  },
  "equity": {
    "current": "215,415,594",
    "change": "-1",
    "after": "215,415,593"
  },
  "initial": {
    "current": "116,965",
    "change": "652",
    "after": "117,617"
  },
  "maintenance": {
    "current": "106,332",
    "change": "592",
    "after": "106,924"
  },
  "position": {
    "current": "0",
    "change": "10",
    "after": "10"
  },
  "warn": "21/You are trying to submit an order without having market data for this instrument. \nIB strongly recommends against this kind of blind trading which may result in \nerroneous or unexpected trades.",
  "error": null
}
Overnight Order Submission
Copy Location
Trading with the WebAPI allows users to submit orders in the
OVERNIGHT market
using both OVERNIGHT exclusive orders as well as OVERNIGHT+DAY orders. This is handled by submitting the affiliated Time-In-Force value when
Placing an Order
.
Overnight
Overnight orders are submitted using the “OVT” time in force value.
{ "tif": "OVT" }
Overnight+DAY
Overnight+DAY orders are submitted using the “OND” time in force value.
{ "tif": "OND" }
Bracket Orders & OCA Groups
Copy Location
The available values and structures of Bracket or OCA orders follow the same general structure of individual orders. Bracket and OCA orders require a parent order be submitted, and then each leg, or child order, would include the parent’s order ID.
Bracket orders can be submitted sequentially using the default order_id created by Interactive Brokers.
OR
Bracket orders can be submitted using the cOID field for the parent order, and then use this same value in each of the child orders in the parentId field.
The body content on the right represents a standard bracket order which contains a parent order, a profit taker, and a stop loss. As you can see, the only addition to this order is the inclusion of cOID in the parent order, and the parentId field in the two children.
{
  "orders": [
    {
      "acctId": "U1234567",
      "conid": 265598,
      "cOID": "Parent",
      "orderType": "MKT",
      "listingExchange": "SMART",
      "outsideRTH": true,
      "side": "Buy",
      "referrer": "QuickTrade",
      "tif": "GTC",
      "quantity": 50
    },
    {
      "acctId": "U1234567",
      "conid": 265598,
      "orderType": "STP",
      "listingExchange": "SMART",
      "outsideRTH": false,
      "price": 157.30,
      "side": "Sell",
      "tif": "GTC",
      "quantity": 50,
      "parentId": "Parent"
    },
    {
      "acctId": "U1234567",
      "conid": 265598,
      "orderType": "LMT",
      "listingExchange": "SMART",
      "outsideRTH": false,
      "price": 157.00,
      "side": "Sell",
      "tif": "GTC",
      "quantity": 50,
      "parentId": "Parent"
    }
  ]
}
An OCA group will follow this same structure. However, in addition to the standard bracket, each order will include
"isSingleGroup": true
. Otherwise, no additional modifications need to be made.
{
  "orders": [
    {
      "acctId": "U1234567",
      "conid": 265598,
      "cOID": "Parent",
      "orderType": "MKT",
      "listingExchange": "SMART",
      "isSingleGroup": true,
      "outsideRTH": true,
      "side": "Buy",
      "referrer": "QuickTrade",
      "tif": "GTC",
      "quantity": 50
    },
    {
      "acctId": "U1234567",
      "conid": 265598,
      "orderType": "STP",
      "listingExchange": "SMART",
      "isSingleGroup": true,
      "outsideRTH": false,
      "price": 157.30,
      "side": "Sell",
      "tif": "GTC",
      "quantity": 50,
      "parentId": "Parent"
    },
    {
      "acctId": "U1234567",
      "conid": 265598,
      "orderType": "LMT",
      "listingExchange": "SMART",
      "outsideRTH": false,
      "isSingleGroup": true,
      "price": 157.00,
      "side": "Sell",
      "tif": "GTC",
      "quantity": 50,
      "parentId": "Parent"
    }
  ]
}
Combo / Spread Orders
Copy Location
Combination orders or spread orders may also be placed using the same orders endpoint. In the case of combo orders, we must use the ‘conidex’ instead of “conid”. The conidex field is a string representation of our combo order parameters.
Combo Orders follow the format of: ‘{spread_conid};;;{leg_conid1}/{ratio},{leg_conid2}/{ratio}‘
The spread_conid is a unique identified used to denote a spread order.  For US Stock Combos, only the spread_conid needs to be submitted.. For all other countries, you will need to use the format ‘spread_conid@exchange’.
Available currency spread conids:
Currency
Spread ConID
AUD
61227077
CAD
61227082
CHF
61227087
CNH
136000441
GBP
58666491
HKD
61227072
INR
136000444
JPY
61227069
KRW
136000424
MXN
136000449
SEK
136000429
SGD
426116555
USD
28812380
Following our spread_conid, we will then follow with 3 semicolons, and then the first leg_coind. This will be the first contract to trade. After the conid, a forward slash, ‘/’, needs to be included followed by your spread ratio.
The ratio indicates two parts. The first is the sign of the ratio, whether it is positive or negative. Positive signs indicate a ‘Buy’ side, while a negative value represents a ‘Sell’ side. This could also be explained as a state of ‘Long’ and ‘Short’ respectively, depending on your current position and intention. After indicating the side, you would indicate the ratio value. This is the multiplier of your quantity value.
Now, you can continue to add legs to the order by separating them with a comma. The number of legs available is based on the exchange’s rules.
Combo Order Pricing
Combo orders can submit their price values based on the value of the individual leg, multiplied by the ratio. Each leg is then added together to create the final price of the order.
These prices can end as negative values if one of the legs is being sold and the total value of that leg multiplied by the ratio is greater than the value of the other order.
The price of a combo order = [({Cost of Leg 1} * {The ratio of Leg 1}) + ({Cost of Leg n} * {Ratio of Leg n}) + ({Cost of Leg n+1} * {Ratio of Leg n+1})]
Cancel Order
Copy Location
Cancels an open order.
Must call /iserver/accounts endpoint prior to cancelling an order.
Use /iservers/account/orders endpoint to review open-order(s) and get latest order status.
DELETE /iserver/account/{{ accountId }}/order/{{ orderId }}
Request Object
Path Param
accountId:
String.
The account ID for which account should place the order.
orderId:
String.
The orderID for that should be modified.
Can be retrieved from /iserver/account/orders
Submitting ‘-1’ will cancel all open orders
Query Param
manualIndicator:
Boolean.
Required*
IMPORTANT
This field is required when trading Futures and Futures Options contracts to remain in compliance with
CME Group Rule 536-B
.
The Manual Order Indicator is used to determine if an order was manually entered or handled through an automated tool. Regardless of original submission, the cancellation must also include the manualIndicator tag to signify of the order cancellation was done manually or autonomously.
true indicates the order was cancellation manually through an interface while false indicates an order was cancellation through an automated system.
extOperator:
string.
Required*
IMPORTANT
This field is required when trading Futures and Futures Options contracts to remain in compliance with
CME Group Rule 536-B
.
The External Operator field should contain information regarding the submitting user in charge of the API operation at the time of request submission.
Python
cURL
request_url = f”{baseUrl}/iserver/account/U1234567/order/123456789?manualIndicator=true&extOperator=person1234″
requests.delete(url=request_url)
curl \
--url {{baseUrl}}/iserver/account/U1234567/order/123456789?manualIndicator=true&extOperator=person1234 \
--request DELETE
Response Object
msg:
String.
Returns the confirmation of the request being submitted.
order_id:
int.
Returns the orderID of the cancelled order.
conid:
int.
Returns the conid for the requested order to be cancelled.
Returns -1 for orders that were immediately cancelled on request.
account:
String.
Returns the accountId for the requested order to be cancelled.
Returns null for orders that were immediately cancelled on request.
{
    "msg": "Request was submitted",
    "order_id": 123456789,
    "conid": 265598,
    "account": "U1234567"
}
Error Object
error:
String.
Returns the error message.
{
    "error": "OrderID 1 doesn't exist"
}
Modify Order
Copy Location
Modifies an open order.
Must call /iserver/accounts endpoint prior to modifying an order.
Use /iservers/account/orders endpoint to review open-order(s).
POST /iserver/account/{accountId}/order/{orderId}
Request Object
Path Param
accountId:
String.
The account ID for which account should place the order.
orderId:
String.
The orderID for that should be modified.
Can be retrieved from /iserver/account/orders
Body Params
The body content of the modify order endpoint will follow the same structure as the standard /iserver/account/{accountId}/orders endpoint.
The content should mirror the content of the original order.
manualIndicator:
boolean.
Required*
IMPORTANT
This field is required when trading Futures and Futures Options contracts to remain in compliance with
CME Group Rule 536-B
.
The Manual Order Indicator is used to determine if an order was modified manually or through an automated tool. Regardless of original submission, the modification must also include the manualIndicator tag to signify of the order modification was manual or automated.
true indicates the order was modified manually through an interface while false indicates an order was modified through an automated system.
extOperator:
string.
Required*
IMPORTANT
This field is required when trading Futures and Futures Options contracts to remain in compliance with
CME Group Rule 536-B
.
The External Operator field should contain information regarding the submitting user in charge of the API operation at the time of request submission.
See the
Place Order
section for more details.
Python
cURL
request_url = f"{baseUrl}/iserver/account/U1234567/order/123456789
json_content = {
  "conid": 265598,
  "orderType": "STP",
  "price": 180,
  "side": "BUY",
  "tif": "DAY",
  "quantity": 10,
  "manualIndicator":True,
  "extOperator": "person1234"
}
requests.post(url=request_url, json=json_content)
curl \
--url {{baseUrl}}/iserver/account/U1234567/order/123456789 \
--request POST \
--header 'Content-Type:application/json' \
--data '{
  "conid": 265598,
  "orderType": "STP",
  "price": 180,
  "side": "BUY",
  "tif": "DAY",
  "quantity": 10,
  "manualIndicator":true,
  "extOperator": "person1234"

}'
Response Object
orderId:
String.
Returns the orders identifier which can be used for order tracking, modification, and cancellation.
order_status:
String.
Returns the order status of the current market order.
See
Order Status Value
for more information.
encrypt_message:
String.
Returns a “1” to display that the message sent was encrypted.
[
    {
        "order_id": "1234567890",
        "order_status": "Submitted",
        "encrypt_message": "1"
    }
]
Alternate Response Object
In some instances, you will receive an ID along with a message about your order.
See the
Place Order Reply
section for more details on resolving the confirmation.
id:
String.
Returns a message ID relating to the particular order’s warning confirmation.
message:
Array of Strings.
Returns the message warning about why the order wasn’t initially transmitted.
isSuppressed:
bool.
Returns if a particular warning was suppressed before sending.
Always returns false.
messageIds:
Array of Strings.
Returns an internal message identifier (Internal use only).
[
  {
    "id": "a12b34c5-d678-9e012f-3456-7a890b12cd3e",
    "message": [
      "You are about to submit a stop order. Please be aware of the various stop order types available and the risks associated with each one.\nAre you sure you want to submit this order?"
    ],
    "isSuppressed": false,
    "messageIds": [
      "o0"
    ]
  }
]
Suppress Messages
Copy Location
Disables a messageId, or series of messageIds, that will no longer prompt the user.
POST /iserver/questions/suppress
Request Object
Body Param
messageIds:
Array of Strings.
The identifier for each warning message to suppress.
The array supports up to 51 messages sent in a single request. Any additional values will result in a system error.
The only supported message IDs are listed in our
Suppressible Message IDs
list. However, users should look to suppress messages on an as-needed basis to avoid unexpected order submissions.
Python
cURL
request_url = f"{baseUrl}/iserver/questions/suppress"
json_content = {
  "messageIds": ["o102"]
}
requests.post(url=request_url, json=json_content)
curl \
--url {{baseUrl}}/iserver/questions/suppress \
--request POST \
--header 'Content-Type:application/json' \
--data '{
  "messageIds": ["o102"]
}'
Response Object
status:
String.
Verifies that the request has been sent.
{
  "status": "submitted"
}
Suppressible MessageIds
Copy Location
MessageId
Text
o163
The following order exceeds the price percentage limit
o354
“””You are submitting an order without market data. We strongly recommend against this as it may result in erroneous and unexpected trades. Are you sure you want to submit this order?”””
o382
The following value exceeds the tick size limit
o383
“””The following order \””BUY 650 AAPL NASDAQ.NMS\”” size exceeds the Size Limit of 500.\nAre you sure you want to submit this order?”””
o403
“””This order will most likely trigger and fill immediately.\nAre you sure you want to submit this order?”””
o451
“””The following order \””BUY 650 AAPL NASDAQ.NMS\”” value estimate of 124,995.00 USD exceeds \nthe Total Value Limit of 100,000 USD.\nAre you sure you want to submit this order?”””
o2136
Mixed allocation order warning
o2137
Cross side order warning
o2165
Warns that instrument does not support trading in fractions outside regular trading hours
o10082
Called Bond warning
o10138
The following order size modification exceeds the size modification limit.
o10151
Warns about risks with Market Orders
o10152
Warns about risks associated with stop orders once they become active
o10153
<h4>Confirm Mandatory Cap Price</h4>To avoid trading at a price that is not consistent with a fair and orderly market, IB may set a cap (for a buy order) or sell order). THIS MAY CAUSE AN ORDER THAT WOULD OTHERWISE BE MARKETABLE TO NOT BE TRADED.
o10164
“””Traders are responsible for understanding cash quantity details, which are provided on a best efforts basis only.”””
o10223
“””<h4>Cash Quantity Order Confirmation</h4>Orders that express size using a monetary value (cash quantity) are provided on a non-guaranteed basis. The system simulates the order by cancelling it once the specified amount is spent (for buy orders) or collected (for sell orders). In addition to the monetary value, the order uses a maximum size that is calculated using the Cash Quantity Estimate Factor, which you can modify in Presets.”””
o10288
Warns about risks associated with market orders for Crypto
o10331
“””You are about to submit a stop order. Please be aware of the various stop order types available and the risks associated with each one.\nAre you sure you want to submit this order?”””
o10332
OSL Digital Securities LTD Crypto Order Warning
o10333
Option Exercise at the Money warning
o10334
Warns that order will be placed into current omnibus account instead of currently selected global account.
o10335
Serves internal Rapid Entry window.
o10336
This security has limited liquidity. If you choose to trade this security, there is a heightened risk that you may not be able to close your position at the time you wish, at a price you wish, and/or without incurring a loss. Confirm that you understand the risks of trading illiquid securities.
Are you sure you want to submit this order?
p6
This order will be distributed over multiple accounts. We strongly suggest you familiarize yourself with our allocation facilities before submitting orders.
p12
“If your order is not immediately executable, our systems may, depending on market conditions, reject your order if its limit price is more than the allowed amount away from the reference price at that time. If this happens, you will not receive a fill. This is a control designed to ensure that we comply with our regulatory obligations to avoid submitting disruptive orders to the marketplace.\\nUse the Price Management Algo?”
Reset Suppressed Messages
Copy Location
Resets all messages disabled by the
Suppress Messages endpoint
.
POST /iserver/questions/suppress/reset
Request Object
No params or body content should be sent.
Python
cURL
request_url = f"{baseUrl}/iserver/questions/suppress/reset"
json_content = {}
requests.post(url=request_url, json=json_content)
curl \
--url {{baseUrl}}/iserver/questions/suppress/reset \
--request POST \
--header 'Content-Type:application/json' \
--data ''
Response Object
status:
String.
Verifies that the request has been sent.
{
  "status": "submitted"
}
Portfolio
Copy Location
Portfolio Accounts
Copy Location
In non-tiered account structures, returns a list of accounts for which the user can view position and account information. This endpoint must be called prior to calling other /portfolio endpoints for those accounts. For querying a list of accounts which the user can trade, see /iserver/accounts. For a list of subaccounts in tiered account structures (e.g. financial advisor or ibroker accounts) see /portfolio/subaccounts.
GET /portfolio/accounts
Request Object
No params or body content should be sent.
Python
cURL
request_url = f"{baseUrl}/portfolio/accounts"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/portfolio/accounts \
--request GET
Response Object
id:
String
The account ID for which account should place the order.
accountId:
String
The account ID for which account should place the order.
accountVan:
String
The account alias for which account should place the order.
accountTitle:
String
Title of the account
displayName:
String
The account ID for which account should place the order.
accountAlias:
String
User customizable account alias. Refer to Configure Account Alias for details.
accountStatus:
int.
When the account was opened in unix time.
currency:
String
Base currency of the account.
type:
String
Account Type
tradingType:
String
Account trading structure.
businessType:
String.
Returns the organizational strcuture of the account.
ibEntity:
String.
Returns the entity of Interactive Brokers the account is tied to.
faClient:
bool.
If an account is a sub-account to a Financial Advisor.
clearingStatus:
String
Status of the Account
Potential Values: O: Open; P or N: Pending; A: Abandoned; R: Rejected; C: Closed.
covestor:
bool.
Is a Covestor Account
noClientTrading:
bool.
Returns if the client account may trade.
trackVirtualFXPortfolio:
bool.
Returns if the account is tracking Virtual FX or not.
parent:
{
mmc:
Array of Strings.
Returns the Money Manager Client Account.
accountId:
String
Account Number for Money Manager Client
isMParent:
bool.
Returns if this is a Multiplex Parent Account
isMChild:
bool.
Returns if this is a Multiplex Child Account
isMultiplex:
bool.
Is a Multiplex Account. These are account models with individual account being parent and managed account being child.
}
desc:
String
Returns an account description.
Value Format: “accountId – accountAlias”
}]
[
  {
    "id": "U1234567",
    "PrepaidCrypto-Z": false,
    "PrepaidCrypto-P": false,
    "brokerageAccess": true,
    "accountId": "U1234567",
    "accountVan": "U1234567",
    "accountTitle": "",
    "displayName": "U1234567",
    "accountAlias": null,
    "accountStatus": 1644814800000,
    "currency": "USD",
    "type": "DEMO",
    "tradingType": "PMRGN",
    "businessType": "IB_PROSERVE",
    "ibEntity": "IBLLC-US",
    "faclient": false,
    "clearingStatus": "O",
    "covestor": false,
    "noClientTrading": false,
    "trackVirtualFXPortfolio": true,
    "parent": {
      "mmc": [],
      "accountId": "",
      "isMParent": false,
      "isMChild": false,
      "isMultiplex": false
    },
    "desc": "U1234567"
  }
]
Portfolio Subaccounts
Copy Location
Used in tiered account structures (such as Financial Advisor and IBroker Accounts) to return a list of up to 100 sub-accounts for which the user can view position and account-related information. This endpoint must be called prior to calling other /portfolio endpoints for those sub-accounts. If you have more than 100 sub-accounts use /portfolio/subaccounts2. To query a list of accounts the user can trade, see /iserver/accounts.
GET /portfolio/subaccounts
Request Object
No params or body content should be sent.
Python
cURL
request_url = f"{baseUrl}/portfolio/subaccounts"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/portfolio/subaccounts \
--request GET
Response Object
id:
String
The account ID for which account should place the order.
accountId:
String
The account ID for which account should place the order.
accountVan:
String
The account alias for which account should place the order.
accountTitle:
String
Title of the account
displayName:
String
The account ID for which account should place the order.
accountAlias:
String
User customizable account alias. Refer to Configure Account Alias for details.
accountStatus:
int.
When the account was opened in unix time.
currency:
String
Base currency of the account.
type:
String
Account Type
tradingType:
String
Account trading structure.
businessType:
String.
Returns the organizational strcuture of the account.
ibEntity:
String.
Returns the entity of Interactive Brokers the account is tied to.
faClient:
bool.
If an account is a sub-account to a Financial Advisor.
clearingStatus:
String
Status of the Account
Potential Values: O: Open; P or N: Pending; A: Abandoned; R: Rejected; C: Closed.
covestor:
bool.
Is a Covestor Account
noClientTrading:
bool.
Returns if the client account may trade.
trackVirtualFXPortfolio:
bool.
Returns if the account is tracking Virtual FX or not.
parent:
{
mmc:
Array of Strings.
Returns the Money Manager Client Account.
accountId:
String
Account Number for Money Manager Client
isMParent:
bool.
Returns if this is a Multiplex Parent Account
isMChild:
bool.
Returns if this is a Multiplex Child Account
isMultiplex:
bool.
Is a Multiplex Account. These are account models with individual account being parent and managed account being child.
}
desc:
String
Returns an account description.
Value Format: “accountId – accountAlias”
}]
[
  {
    "id": "U1234567",
    "PrepaidCrypto-Z": false,
    "PrepaidCrypto-P": false,
    "brokerageAccess": false,
    "accountId": "U1234567",
    "accountVan": "U1234567",
    "accountTitle": "",
    "displayName": "U1234567",
    "accountAlias": null,
    "accountStatus": 1644814800000,
    "currency": "USD",
    "type": "DEMO",
    "tradingType": "PMRGN",
    "businessType": "IB_PROSERVE",
    "ibEntity": "IBLLC-US",
    "faclient": false,
    "clearingStatus": "O",
    "covestor": false,
    "noClientTrading": false,
    "trackVirtualFXPortfolio": true,
    "parent": {
      "mmc": [],
      "accountId": "",
      "isMParent": false,
      "isMChild": false,
      "isMultiplex": false
    },
    "desc": "U1234567"
  }
]
Portfolio Subaccounts (Large Account Structures)
Copy Location
Used in tiered account structures (such as Financial Advisor and IBroker Accounts) to return a list of sub-accounts, paginated up to 20 accounts per page, for which the user can view position and account-related information. This endpoint must be called prior to calling other /portfolio endpoints for those sub-accounts. If you have less than 100 sub-accounts use /portfolio/subaccounts. To query a list of accounts the user can trade, see /iserver/accounts.
GET /portfolio/subaccounts2
Request Object
page:
String.
Required
Indicate the page identifier that should be retrieved.
Pagination begins at page 0.
20 accounts returned per page.
Python
cURL
request_url = f"{baseUrl}/portfolio/subaccounts2?page=0"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/portfolio/subaccounts2?page=0 \
--request GET
Response Object
metadata:
Object.
Contains metadata about the response data.
{
total:
int.
Displays the total number of accounts returned.
pageSize:
int.
Returns the page size.
pageNum:
int.
Returns the page number or identifier of the request.
subaccounts:
Array of Objects.
Contains all of the accounts and their respective data.
[{
id:
String
The account ID for which account should place the order.
accountId:
String
The account ID for which account should place the order.
accountVan:
String
The account alias for which account should place the order.
accountTitle:
String
Title of the account
displayName:
String
The account ID for which account should place the order.
accountAlias:
String
User customizable account alias. Refer to Configure Account Alias for details.
accountStatus:
int.
When the account was opened in unix time.
currency:
String
Base currency of the account.
type:
String
Account Type
tradingType:
String
Account trading structure.
businessType:
String.
Returns the organizational strcuture of the account.
ibEntity:
String.
Returns the entity of Interactive Brokers the account is tied to.
faClient:
bool.
If an account is a sub-account to a Financial Advisor.
clearingStatus:
String
Status of the Account
Potential Values: O: Open; P or N: Pending; A: Abandoned; R: Rejected; C: Closed.
covestor:
bool.
Is a Covestor Account
noClientTrading:
bool.
Returns if the client account may trade.
trackVirtualFXPortfolio:
bool.
Returns if the account is tracking Virtual FX or not.
parent:
{
mmc:
Array of Strings.
Returns the Money Manager Client Account.
accountId:
String
Account Number for Money Manager Client
isMParent:
bool.
Returns if this is a Multiplex Parent Account
isMChild:
bool.
Returns if this is a Multiplex Child Account
isMultiplex:
bool.
Is a Multiplex Account. These are account models with individual account being parent and managed account being child.
}
desc:
String
Returns an account description.
Value Format: “accountId – accountAlias”
}]
[
  {
    "id": "U1234567",
    "PrepaidCrypto-Z": false,
    "PrepaidCrypto-P": false,
    "brokerageAccess": false,
    "accountId": "U1234567",
    "accountVan": "U1234567",
    "accountTitle": "",
    "displayName": "U1234567",
    "accountAlias": null,
    "accountStatus": 1644814800000,
    "currency": "USD",
    "type": "DEMO",
    "tradingType": "PMRGN",
    "businessType": "IB_PROSERVE",
    "ibEntity": "IBLLC-US",
    "faclient": false,
    "clearingStatus": "O",
    "covestor": false,
    "noClientTrading": false,
    "trackVirtualFXPortfolio": true,
    "parent": {
      "mmc": [],
      "accountId": "",
      "isMParent": false,
      "isMChild": false,
      "isMultiplex": false
    },
    "desc": "U1234567"
  }
]
Specific Account's Portfolio Information
Copy Location
Account information related to account Id /portfolio/accounts or /portfolio/subaccounts must be called prior to this endpoint.
GET /portfolio/{accountId}/meta
Request Object
Path Params
accountId:
String.
Required
Specify the AccountID to receive portfolio information for.
Python
cURL
request_url = f"{baseUrl}/portfolio/U1234567/meta"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/portfolio/U1234567/meta \
--request GET
Response Object
id:
String
The account ID for which account should place the order.
accountId:
String
The account ID for which account should place the order.
accountVan:
String
The account alias for which account should place the order.
accountTitle:
String
Title of the account
displayName:
String
The account ID for which account should place the order.
accountAlias:
String
User customizable account alias. Refer to Configure Account Alias for details.
accountStatus:
int.
When the account was opened in unix time.
currency:
String
Base currency of the account.
type:
String
Account Type
tradingType:
String
Account trading structure.
businessType:
String.
Returns the organizational strcuture of the account.
ibEntity:
String.
Returns the entity of Interactive Brokers the account is tied to.
faClient:
bool.
If an account is a sub-account to a Financial Advisor.
clearingStatus:
String
Status of the Account
Potential Values: O: Open; P or N: Pending; A: Abandoned; R: Rejected; C: Closed.
covestor:
bool.
Is a Covestor Account
noClientTrading:
bool.
Returns if the client account may trade.
trackVirtualFXPortfolio:
bool.
Returns if the account is tracking Virtual FX or not.
parent:
{
mmc:
Array of Strings.
Returns the Money Manager Client Account.
accountId:
String
Account Number for Money Manager Client
isMParent:
bool.
Returns if this is a Multiplex Parent Account
isMChild:
bool.
Returns if this is a Multiplex Child Account
isMultiplex:
bool.
Is a Multiplex Account. These are account models with individual account being parent and managed account being child.
}
desc:
String
Returns an account description.
Value Format: “accountId – accountAlias”
}]
{
  "id": "U1234567",
  "PrepaidCrypto-Z": false,
  "PrepaidCrypto-P": false,
  "brokerageAccess": false,
  "accountId": "U1234567",
  "accountVan": "U1234567",
  "accountTitle": "",
  "displayName": "U1234567",
  "accountAlias": null,
  "accountStatus": 1644814800000,
  "currency": "USD",
  "type": "DEMO",
  "tradingType": "PMRGN",
  "businessType": "IB_PROSERVE",
  "ibEntity": "IBLLC-US",
  "faclient": false,
  "clearingStatus": "O",
  "covestor": false,
  "noClientTrading": false,
  "trackVirtualFXPortfolio": true,
  "parent": {
    "mmc": [],
    "accountId": "",
    "isMParent": false,
    "isMChild": false,
    "isMultiplex": false
  },
  "desc": "U1234567"
}
Portfolio Allocation (Single)
Copy Location
Information about the account’s portfolio allocation by Asset Class, Industry and Category. /portfolio/accounts or /portfolio/subaccounts must be called prior to this endpoint.
GET /portfolio/{accountId}/allocation
Request Object
Path Params
accountId:
String.
Required
Specify the account ID for the request.
Python
cURL
request_url = f"{baseUrl}/portfolio/U1234567/allocation"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/portfolio/U1234567/allocation \
--request GET
Response Object
assetClass:
Object.
Contains details pertaining to specific security types.
{
long:
Object.
Returns the value of the asset class currently traded long.
short:
Object.
Returns the value of the asset class currently traded short.
},
sector:
Object.
Contains details pertaining to specific trade sectors.
{
long:
Object.
Returns the value of the trade sector currently traded long.
short:
Object.
Returns the value of the trade sector currently traded short.
},
group:
Object.
Contains details pertaining to specific industry groups.
{
long:
Object.
Returns the value of the industry group currently traded long.
short:
Object.
Returns the value of the industry group currently traded short.
}
{
  "assetClass": {
    "long": {
      "OPT": 27.12,
      "STK": 317071.39468663215,
      "CASH": 2.1510110008312488E8
    },
    "short": {
      "OPT": -30.0,
      "CASH": -25.917167515158653
    }
  },
  "sector": {
    "long": {
      "Others": 5628.650040692091,
      "Technology": 237511.16,
      "Industrial": 43134.63,
      "Consumer, Cyclical": 22537.62620745659,
      "Financial": 2504.35,
      "Communications": 5116.61,
      "Consumer, Non-cyclical": 665.4884384834767
    },
    "short": {
      "Others": -30.0
    }
  },
  "group": {
    "long": {
      "Computers": 121517.38,
      "Others": 5628.650040692091,
      "Semiconductors": 115993.78,
      "Auto Manufacturers": 22537.62620745659,
      "Banks": 2504.35,
      "Miscellaneous Manufactur": 43134.63,
      "Internet": 5116.61,
      "Beverages": 649.07,
      "Pharmaceuticals": 16.41843848347664
    },
    "short": {
      "Others": -30.0
    }
  }
}
Combination Positions
Copy Location
Provides all positions held in the account acquired as a combination, including values such as ratios, size, and market value.
GET /portfolio/{accountId}/combo/positions
Request Object
Path Params
accountId:
String. Required
The account ID for which account should place the order.
Query Param
nocache:
Boolean
Set if request should be made without caching.
Defaults to false
Python
cURL
request_url = f"{baseUrl}/portfolio/U1234567/combo/positions?nocache=true"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/portfolio/U1234567/combo/positions?nocache=true \
--request GET
Response Object
name:
String.
This is an internal name used to distinguish between combinations.
description:
String.
Provides the ratio and leg conIds for the combo.
legs:
array.
An array containing all legs in the specific combination.
conid:
String.
Returns the conid of one leg of the combo.
ratio:integer
Returns the ratio value for the combo. This can be either positive or negative.
positions:
array.
Provides an array including the leg information in the combo.
acctId:
String.
Returns the accountId holding the leg.
conid:
integer.
Returns the contract ID for the specific leg.
contractDesc:
String.
Returns the long name for the given contract.
position:
integer.
Returns the total size of the specific leg in the combination.
mktPrice:
integer.
Returns the current market price of each share for the leg in the combo.
mktValue:
integer.
Returns the total value of the position in the combo.
currency:
String.
Returns the base currency of the leg.
avgCost:
integer.
Returns the average cost of each share in the position times the multiplier.
avgPrice:
integer.
Returns the average cost of each share in the position when purchased.
realizedPnl:
integer.
Returns the total profit made today through trades.
unrealizedPnl:
integer.
Returns the total potential profit if you were to trade.
exchs:
null.
Deprecated value.
Always returns null.
expiry:
null.
Deprecated value.
Always returns null.
putOrCall:
null.
Deprecated value.
Always returns null.
multiplier:
null.
Deprecated value.
Always returns null.
strike:
integer.
Deprecated value.
Always returns 0.0.
exerciseStyle:
null.
Deprecated value.
Always returns null.
conExchMap:
array.
Deprecated value.
Returns an empty array.
assetClass:
String.
Returns the security type of the leg.
undConid:
integer
Deprecated value.
Always returns 0.
[
  {
    "name":"CP.CP66a00d50",
    "description":"1*708474422-1*710225103",
    "legs":[
      {
        "conid":"708474422",
        "ratio":1
      },
      {
        "conid":"710225103",
        "ratio":-1
      }
    ],
    "positions":[
      {
        "acctId":"U1234567",
        "conid":708474422,
        "contractDesc":"SPX AUG2024 5555 P [SPX 240816P05555000 100]",
        "position":1.0,
        "mktPrice":59.6571617,
        "mktValue":5965.72,
        "currency":"USD",
        "avgCost":6011.70935,
        "avgPrice":60.1170935,
        "realizedPnl":0.0,
        "unrealizedPnl":-45.99,
        "exchs":null,
        "expiry":null,
        "putOrCall":null,
        "multiplier":null,
        "strike":0.0,
        "exerciseStyle":null,
        "conExchMap":[],
        "assetClass":"OPT",
        "undConid":0
      },
      {
        "acctId":"U1234567",
        "conid":710225103,
        "contractDesc":"SPX AUG2024 5565 C [SPX 240816C05565000 100]",
        "position":-1.0,
        "mktPrice":78.02521515,
        "mktValue":-7802.52,
        "currency":"USD",
        "avgCost":7628.29065,
        "avgPrice":76.2829065,
        "realizedPnl":0.0,
        "unrealizedPnl":-174.23,
        "exchs":null,"expiry":null,
        "putOrCall":null,
        "multiplier":null,
        "strike":0.0,
        "exerciseStyle":null,
        "conExchMap":[],
        "assetClass":"OPT",
        "undConid":0
      }
    ]
  }
]
Portfolio Allocation (All)
Copy Location
Similar to /portfolio/{accountId}/allocation but returns a consolidated view of of all the accounts returned by /portfolio/accounts. /portfolio/accounts or /portfolio/subaccounts must be called prior to this endpoint.
POST /portfolio/allocation
Request Object
Body Params
acctIds:
Array of Strings.
Required
Contains all account IDs as strings the user should receive data for.
Python
cURL
request_url = f"{baseUrl}/portfolio/allocation"
json_content = {
  "acctIds": [
    "U1234567",
    "U4567890"
  ]
}
requests.post(url=request_url, json=json_content)
curl \
--url {{baseUrl}}/portfolio/allocation \
--request POST \
--header 'Content-Type:application/json' \
--data '{
  "acctIds": [
    "U1234567",
    "U4567890"
  ]
}'
assetClass:
Object.
Contains details pertaining to specific security types.
{
long:
Object.
Returns the value of the asset class currently traded long.
short:
Object.
Returns the value of the asset class currently traded short.
},
sector:
Object.
Contains details pertaining to specific trade sectors.
{
long:
Object.
Returns the value of the trade sector currently traded long.
short:
Object.
Returns the value of the trade sector currently traded short.
},
group:
Object.
Contains details pertaining to specific industry groups.
{
long:
Object.
Returns the value of the industry group currently traded long.
short:
Object.
Returns the value of the industry group currently traded short.
}
{
  "assetClass": {
    "long": {
      "OPT": 27.12,
      "STK": 316441.2320366,
      "CASH": 2.1510102008312488E8
    },
    "short": {
      "OPT": -30.0,
      "CASH": -25.923946709036827
    }
  },
  "sector": {
    "long": {
      "Others": 5624.600040692091,
      "Technology": 237014.72999999998,
      "Industrial": 43077.12,
      "Consumer, Cyclical": 22453.78620745659,
      "Financial": 2503.3599999999997,
      "Communications": 5126.98,
      "Consumer, Non-cyclical": 667.7757884514332
    },
    "short": {
      "Others": -30.0
    }
  },
  "group": {
    "long": {
      "Computers": 121222.53,
      "Others": 5624.600040692091,
      "Semiconductors": 115792.2,
      "Auto Manufacturers": 22453.78620745659,
      "Banks": 2503.3599999999997,
      "Miscellaneous Manufactur": 43077.12,
      "Internet": 5126.98,
      "Beverages": 651.35,
      "Pharmaceuticals": 16.42578845143318
    },
    "short": {
      "Others": -30.0
    }
  }
}
Positions
Copy Location
Returns a list of positions for the given account.
The endpoint supports paging, each page will return up to 100 positions.
/portfolio/accounts or /portfolio/subaccounts must be called prior to this endpoint.
GET /portfolio/{accountId}/positions/{pageId}
Request Object
Path Params
accountId:
String.
Required
The account ID for which account should place the order.
pageId:
String.
Required
The “page” of positions that should be returned.
One page contains a maximum of 100 positions.
Pagination starts at 0.
Query Params
model:
String.
Code for the model portfolio to compare against.
sort:
String.
Declare the table to be sorted by which column
direction:
String.
The order to sort by.
‘a’ means ascending
‘d’ means descending
period:
String.
period for pnl column
Value Format: 1D, 7D, 1M
Python
cURL
request_url = f"{baseUrl}/portfolio/U1234567/positions/0?direction=a&period=1W&sort=position&model=MyModel"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/portfolio/U1234567/positions/0?direction=a&period=1W&sort=position&model=MyModel \
--request GET
Response Object
acctId:
String.
conid:
int.
Returns the contract ID of the position.
contractDesc:
String.
Returns the local symbol of the order.
position:
float.
Returns the total size of the position.
mktPrice:
float.
Returns the current market price of each share.
mktValue:
float.
Returns the total value of the order.
avgCost:
float.
Returns the average cost of each share in the position times the multiplier.
avgPrice:
float.
Returns the average cost of each share in the position when purchased.
realizedPnl:
float.
Returns the total profit made today through trades.
unrealizedPnl:
float.
Returns the total potential profit if you were to trade.
exchs:
null.
Deprecated value.
Always returns null.
currency:
String.
Returns the traded currency for the contract.
time:
int.
Returns amount of time in ms to generate the data.
chineseName:
String.
Returns the Chinese characters for the symbol.
allExchanges:
String*.
Returns a series of exchanges the given symbol can trade on.
listingExchange:
String.
Returns the primary or listing exchange the contract is hosted on.
countryCode:
String.
Returns the country code the contract is traded on.
name:
String.
Returns the comapny name.
assetClass:
String.
Returns the asset class or security type of the contract.
expiry:
String.
Returns the expiry of the contract. Returns null for non-expiry instruments.
lastTradingDay:
String.
Returns the last trading day of the contract.
group:
String.
Returns the group or industry the contract is affilated with.
putOrCall:
String.
Returns if the contract is a Put or Call option.
sector:
String.
Returns the contract’s sector.
sectorGroup:
String.
Returns the sector’s group.
strike:
String.
Returns the strike of the contract.
ticker:
String.
Returns the ticker symbol of the traded contract.
undConid:
int.
Returns the contract’s underlyer.
multiplier:
float,
Returns the contract multiplier.
type:
String.
Returns stock type.
hasOptions:
bool.
Returns if contract has tradable options contracts.
fullName:
String.
Returns symbol name for requested contract.
isUS:
bool.
Returns if the contract is US based or not.
incrementRules:
Array.
Returns rules regarding incrementation for market data and order placemnet.
lowerEdge:
float,
Returns lower edge value used to calculate increment.
increment:
float.
Allowed incrementable value.
displayRule:
object.
Returns an object containing display content for market data.
magnification:
int.
Returns maginification or multiplier of contract
displayRuleStep:
Array.
Contains various rules in the display object.
decimalDigits:
int.
Returns average decimal digit for data display.
lowerEdge:
float.
Returns lower edge value used to calculate increment.
wholeDigits:
int.
Returns allowed display size.
isEventContract:
bool.
Returns if the contract is an event contract or not.
pageSize:
int.
Returns the content size of the request.
}]
[
  {
    "acctId": "U1234567",
    "conid": 756733,
    "contractDesc": "SPY",
    "position": 5.0,
    "mktPrice": 471.16000365,
    "mktValue": 2355.8,
    "currency": "USD",
    "avgCost": 434.93,
    "avgPrice": 434.93,
    "realizedPnl": 0.0,
    "unrealizedPnl": 181.15,
    "exchs": null,
    "expiry": null,
    "putOrCall": null,
    "multiplier": null,
    "strike": 0.0,
    "exerciseStyle": null,
    "conExchMap": [],
    "assetClass": "STK",
    "undConid": 0,
    "model": ""
  },
  {
    "acctId": "U1234567",
    "conid": 76792991,
    "contractDesc": "TSLA",
    "position": 7.0,
    "mktPrice": 250.73399355,
    "mktValue": 1755.14,
    "currency": "USD",
    "avgCost": 221.67142855,
    "avgPrice": 221.67142855,
    "realizedPnl": 0.0,
    "unrealizedPnl": 203.44,
    "exchs": null,
    "expiry": null,
    "putOrCall": null,
    "multiplier": null,
    "strike": 0.0,
    "exerciseStyle": null,
    "conExchMap": [],
    "assetClass": "STK",
    "undConid": 0,
    "model": ""
  },
  {
    "acctId": "U1234567",
    "conid": 107113386,
    "contractDesc": "META",
    "position": 11.0,
    "mktPrice": 333.1199951,
    "mktValue": 3664.32,
    "currency": "USD",
    "avgCost": 306.6909091,
    "avgPrice": 306.6909091,
    "realizedPnl": 0.0,
    "unrealizedPnl": 290.72,
    "exchs": null,
    "expiry": null,
    "putOrCall": null,
    "multiplier": null,
    "strike": 0.0,
    "exerciseStyle": null,
    "conExchMap": [],
    "assetClass": "STK",
    "undConid": 0,
    "model": ""
  }
]
Positions (NEW)
Copy Location
Returns a list of positions for the given account.
/portfolio/accounts or /portfolio/subaccounts must be called prior to this endpoint.
This endpoint provides near-real time updates and removes caching otherwise found in the /portfolio/{accountId}/positions/{pageId} endpoint.
GET /portfolio2/{accountId}/positions
Request Object
Path Params
accountId:
String.
Required
The account ID for which account should place the order.
Query Params
model:
String.
Code for the model portfolio to compare against.
sort:
String.
Declare the table to be sorted by which column
direction:
String.
The order to sort by.
‘a’ means ascending
‘d’ means descending
Python
cURL
request_url = f"{baseUrl}/portfolio2/U1234567/positions?direction=a&sort=position"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/portfolio2/U1234567/positions?direction=a&sort=position \
--request GET
Response Object
position:
float.
Returns the total size of the position.
conid:
int.
Returns the contract ID of the position.
avgCost:
float.
Returns the average cost of each share in the position times the multiplier.
avgPrice:
float.
Returns the average cost of each share in the position when purchased.
currency:
String.
Returns the traded currency for the contract.
description:
String.
Returns the local symbol of the order.
isLastToLoq:
String.
Returns if the contract is last to liquidate.
mktPrice:
float.
Returns the current market price of each share.
mktValue:
float.
Returns the total value of the order.
realizedPnl:
float.
Returns the total profit made today through trades.
unrealizedPnl:
float.
Returns the total potential profit if you were to trade.
secType:
String.
Returns the asset class or security type of the contract.
timestamp:
integer.
Returns the epoch timestamp of the portfolio request.
assetClass:
String.
Returns the asset class or security type of the contract.
sector:
String.
Returns the contract’s sector.
group:
String.
Returns the group or industry the contract is affilated with.
model
: String.
Code for the model portfolio to compare against.
{
“position”: 12.0,
“conid”: “9408”,
“avgCost”: 266.20888333333335,
“avgPrice”: 266.20888333333335,
“currency”: “USD”,
“description”: “MCD”,
“isLastToLoq”: false,
“marketPrice”: 258.8299865722656,
“marketValue”: 3105.9598388671875,
“realizedPnl”: 0.0,
“secType”: “STK”,
“timestamp”: 1717444668,
“unrealizedPnl”: 88.54676113281266,
“assetClass”: “STK”,
“sector”: “Consumer, Cyclical”,
“group”: “Retail”,
“model”: “”
}
Positions by Conid
Copy Location
Returns a list containing position details only for the specified conid.
The initial request will return exclusively the Portfolio information on the contract. Sequential requests for the contract will also return the contract’s information and rules as shown below.
GET /portfolio/{acctId}/position/{conid}
Request Object
Path Params
accountId:
String.
Required
The account ID for which account should place the order.
conId:
String.
Required
The contract ID to receive position information on.
Python
cURL
request_url = f"{baseUrl}/portfolio/U1234567/position/265598"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/portfolio/U1234567/position/265598 \
--request GET
Response Object
acctId:
String.
conid:
int.
Returns the contract ID of the position.
contractDesc:
String.
Returns the local symbol of the order.
position:
float.
Returns the total size of the position.
mktPrice:
float.
Returns the current market price of each share.
mktValue:
float.
Returns the total value of the order.
avgCost:
float.
Returns the average cost of each share in the position times the multiplier.
avgPrice:
float.
Returns the average cost of each share in the position when purchased.
realizedPnl:
float.
Returns the total profit made today through trades.
unrealizedPnl:
float.
Returns the total potential profit if you were to trade.
exchs:
null.
Deprecated value.
Always returns null.
currency:
String.
Returns the traded currency for the contract.
time:
int.
Returns amount of time in ms to generate the data.
chineseName:
String.
Returns the Chinese characters for the symbol.
allExchanges:
String*.
Returns a series of exchanges the given symbol can trade on.
listingExchange:
String.
Returns the primary or listing exchange the contract is hosted on.
countryCode:
String.
Returns the country code the contract is traded on.
name:
String.
Returns the comapny name.
assetClass:
String.
Returns the asset class or security type of the contract.
expiry:
String.
Returns the expiry of the contract. Returns null for non-expiry instruments.
lastTradingDay:
String.
Returns the last trading day of the contract.
group:
String.
Returns the group or industry the contract is affilated with.
putOrCall:
String.
Returns if the contract is a Put or Call option.
sector:
String.
Returns the contract’s sector.
sectorGroup:
String.
Returns the sector’s group.
strike:
int.
Returns the strike of the contract.
ticker:
String.
Returns the ticker symbol of the traded contract.
undConid:
int.
Returns the contract’s underlyer.
multiplier:
float,
Returns the contract multiplier.
type:
String.
Returns stock type.
hasOptions:
bool.
Returns if contract has tradable options contracts.
fullName:
String.
Returns symbol name for requested contract.
isUS:
bool.
Returns if the contract is US based or not.
incrementRules:
Array.
Returns rules regarding incrementation for market data and order placemnet.
lowerEdge:
float,
Returns lower edge value used to calculate increment.
increment:
float.
Allowed incrementable value.
displayRule:
object.
Returns an object containing display content for market data.
magnification:
int.
Returns maginification or multiplier of contract
displayRuleStep:
Array.
Contains various rules in the display object.
decimalDigits:
int.
Retrns average decimal digit for data display.
lowerEdge:
float.
Returns lower edge value used to calculate increment.
wholeDigits:
int.
Returns allowed display size.
isEventContract:
bool.
Returns if the contract is an event contract or not.
pageSize:
int.
Returns the content size of the request.
}]
[
  {
    "acctId": "U1234567",
    "conid": 265598,
    "contractDesc": "AAPL",
    "position": 614.2639,
    "mktPrice": 197.3840027,
    "mktValue": 121245.87,
    "currency": "USD",
    "avgCost": 192.7477563,
    "avgPrice": 192.7477563,
    "realizedPnl": 0.0,
    "unrealizedPnl": 2847.88,
    "exchs": null,
    "expiry": null,
    "putOrCall": null,
    "multiplier": 0.0,
    "strike": "0",
    "exerciseStyle": null,
    "conExchMap": [],
    "assetClass": "STK",
    "undConid": 0,
    "model": "",
    "time": 43,
    "chineseName": "苹果公司",
    "allExchanges": "AMEX,NYSE,CBOE,PHLX,CHX,ARCA,ISLAND,ISE,IDEAL,NASDAQQ,NASDAQ,DRCTEDGE,BEX,BATS,NITEECN,EDGEA,CSFBALGO,JEFFALGO,NYSENASD,PSX,BYX,ITG,PDQ,IBKRATS,CITADEL,NYSEDARK,MIAX,IBDARK,CITADELDP,NASDDARK,IEX,WEDBUSH,SUMMER,WINSLOW,FINRA,LIQITG,UBSDARK,BTIG,VIRTU,JEFF,OPCO,COWEN,DBK,JPMC,EDGX,JANE,NEEDHAM,FRACSHARE,RBCALGO,VIRTUDP,BAYCREST,FOXRIVER,MND,NITEEXST,PEARL,GSDARK,NITERTL,NYSENAT,IEXMID,HRT,FLOWTRADE,HRTDP,JANELP,PEAK6,IMCDP,CTDLZERO,HRTMID,JANEZERO,HRTEXST,IMCLP,LTSE,SOCGENDP,MEMX,INTELCROS,VIRTUBYIN,JUMPTRADE,NITEZERO,TPLUS1,XTXEXST,XTXDP,XTXMID,COWENLP,BARCDP,JUMPLP,OLDMCLP,RBCCMALP,WALLBETH,IBEOS,JONES,GSLP,BLUEOCEAN,USIBSILP,OVERNIGHT,JANEMID,IBATSEOS,HRTZERO,VIRTUALGO",
    "listingExchange": "NASDAQ",
    "countryCode": "US",
    "name": "APPLE INC",
    "lastTradingDay": null,
    "group": "Computers",
    "sector": "Technology",
    "sectorGroup": "Computers",
    "ticker": "AAPL",
    "type": "COMMON",
    "hasOptions": true,
    "fullName": "AAPL",
    "isUS": true,
    "incrementRules": [
      {
        "lowerEdge": 0.0,
        "increment": 0.01
      }
    ],
    "displayRule": {
      "magnification": 0,
      "displayRuleStep": [
        {
          "decimalDigits": 2,
          "lowerEdge": 0.0,
          "wholeDigits": 4
        }
      ]
    },
    "isEventContract": false,
    "pageSize": 100
  }
]
Invalidate Backend Portfolio Cache
Copy Location
Invalidates the cached value for your portfolio’s positions and calls the /portfolio/{accountId}/positions/0 endpoint automatically.
POST /portfolio/{accountId}/positions/invalidate
Request Object
Path Params
accountId:
String.
Required
The account ID for which cache to invalidate.
Python
cURL
request_url = f"{baseUrl}/portfolio/U1234567/positions/invalidate"
json_content = {}
requests.post(url=request_url, json=json_content)
curl \
--url {{baseUrl}}/portfolio/U1234567/positions/invalidate \
--request POST \
--header 'Content-Type:application/json' \
--data '{}'
Response Object
acctId:
String.
conid:
int.
Returns the contract ID of the position.
contractDesc:
String.
Returns the local symbol of the order.
position:
float.
Returns the total size of the position.
mktPrice:
float.
Returns the current market price of each share.
mktValue:
float.
Returns the total value of the order.
avgCost:
float.
Returns the average cost of each share in the position times the multiplier.
avgPrice:
float.
Returns the average cost of each share in the position when purchased.
realizedPnl:
float.
Returns the total profit made today through trades.
unrealizedPnl:
float.
Returns the total potential profit if you were to trade.
exchs:
null.
Deprecated value.
Always returns null.
currency:
String.
Returns the traded currency for the contract.
time:
int.
Returns amount of time in ms to generate the data.
chineseName:
String.
Returns the Chinese characters for the symbol.
allExchanges:
String*.
Returns a series of exchanges the given symbol can trade on.
listingExchange:
String.
Returns the primary or listing exchange the contract is hosted on.
countryCode:
String.
Returns the country code the contract is traded on.
name:
String.
Returns the comapny name.
assetClass:
String.
Returns the asset class or security type of the contract.
expiry:
String.
Returns the expiry of the contract. Returns null for non-expiry instruments.
lastTradingDay:
String.
Returns the last trading day of the contract.
group:
String.
Returns the group or industry the contract is affilated with.
putOrCall:
String.
Returns if the contract is a Put or Call option.
sector:
String.
Returns the contract’s sector.
sectorGroup:
String.
Returns the sector’s group.
strike:
int.
Returns the strike of the contract.
ticker:
String.
Returns the ticker symbol of the traded contract.
undConid:
int.
Returns the contract’s underlyer.
multiplier:
float,
Returns the contract multiplier.
type:
String.
Returns stock type.
hasOptions:
bool.
Returns if contract has tradable options contracts.
fullName:
String.
Returns symbol name for requested contract.
isUS:
bool.
Returns if the contract is US based or not.
incrementRules:
Array.
Returns rules regarding incrementation for market data and order placemnet.
lowerEdge:
float,
Returns lower edge value used to calculate increment.
increment:
float.
Allowed incrementable value.
displayRule:
object.
Returns an object containing display content for market data.
magnification:
int.
Returns maginification or multiplier of contract
displayRuleStep:
Array.
Contains various rules in the display object.
decimalDigits:
int.
Retrns average decimal digit for data display.
lowerEdge:
float.
Returns lower edge value used to calculate increment.
wholeDigits:
int.
Returns allowed display size.
isEventContract:
bool.
Returns if the contract is an event contract or not.
pageSize:
int.
Returns the content size of the request.
}]
[
  {
    "acctId": "U1234567",
    "conid": 265598,
    "contractDesc": "AAPL",
    "position": 614.2639,
    "mktPrice": 197.3840027,
    "mktValue": 121245.87,
    "currency": "USD",
    "avgCost": 192.7477563,
    "avgPrice": 192.7477563,
    "realizedPnl": 0.0,
    "unrealizedPnl": 2847.88,
    "exchs": null,
    "expiry": null,
    "putOrCall": null,
    "multiplier": null,
    "strike": 0.0,
    "exerciseStyle": null,
    "conExchMap": [],
    "assetClass": "STK",
    "undConid": 0,
    "model": ""
  },
  {...},
  {
    "acctId": "U1234567",
    "conid": 8894,
    "contractDesc": "KO",
    "position": 11.0,
    "mktPrice": 59.2400017,
    "mktValue": 651.64,
    "currency": "USD",
    "avgCost": 61.9409091,
    "avgPrice": 61.9409091,
    "realizedPnl": 0.0,
    "unrealizedPnl": -29.71,
    "exchs": null,
    "expiry": null,
    "putOrCall": null,
    "multiplier": null,
    "strike": 0.0,
    "exerciseStyle": null,
    "conExchMap": [],
    "assetClass": "STK",
    "undConid": 0,
    "model": ""
  }
]
Portfolio Summary
Copy Location
Information regarding settled cash, cash balances, etc. in the account’s base currency and any other cash balances hold in other currencies. /portfolio/accounts or /portfolio/subaccounts must be called prior to this endpoint. The list of supported currencies is available at https://www.interactivebrokers.com/en/index.php?f=3185.
GET /portfolio/{accountId}/summary
Request Object
Path Params
accountId:
String.
Required
Specify the account ID for which account you require ledger information on.
Python
cURL
request_url = f"{baseUrl}/portfolio/U1234567/summary"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/portfolio/U1234567/summary \
--request GET
Response Object
The /summary endpoint returns a Key: Value Object structure. This returns a total of 45-135 unique values used to summarize the account.
Responses will come as the base value, containing a summary of all returned details, followed by an identical response name with a trailing “-c” or “-s”. “-c” represents commodity values held under the account. Meanwhile, “-s” represents all security values held on the account.
{object key}:
Object.
This key indicates what data is being returned. This may include account information, balance information, or other relevant portfolio details as specified.
amount:
float.
Returns the price value regarding the key.
May return null if price value not required.
currency:
String.
Returns the base currency the response is built with.
isNull:
bool.
Returns if the value is unavailable.
timestamp:
int.
Returns the time the data was retrieved in epoch time.
value:
String.
Returns a string details about the given key.
May return null if no string value required.
severity:
int.
Internal use only.
{
  "accountcode": {
    "amount": 0.0,
    "currency": null,
    "isNull": false,
    "timestamp": 1702582422000,
    "value": "U1234567",
    "severity": 0
  },
  {...},
  "indianstockhaircut": {
    "amount": 0.0,
    "currency": "USD",
    "isNone": false,
    "timestamp": 1702582422000,
    "value": null,
    "severity": 0
  }
}
Portfolio Ledger
Copy Location
Information regarding settled cash, cash balances, etc. in the account’s base currency and any other cash balances hold in other currencies. /portfolio/accounts or /portfolio/subaccounts must be called prior to this endpoint. The list of supported currencies is available at https://www.interactivebrokers.com/en/index.php?f=3185.
GET /portfolio/{accountId}/ledger
Request Object
Path Params
accountId:
String.
Required
Specify the account ID for which account you require ledger information on.
Python
cURL
request_url = f"{baseUrl}/portfolio/U1234567/ledger"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/portfolio/U1234567/ledger \
--request GET
Response Object
{currency}:
Object.
Returns the ledger values for the specified currency.
May return “BASE” to show your base currency.
{
commoditymarketvalue:
float.
Returns the total market value of commodity positions in the given currency.
futuremarketvalue:
float.
Returns the total market value of futures positions in the given currency.
settledcash:
float.
Returns the total settled cash for the given currency.
exchangerate:
int.
Returns the exchange rate from the base currency to the specified currency.
sessionid:
int.
Internal use only.
cashbalance:
float.
Returns the total cash available for trading in the given currency.
corporatebondsmarketvalue:
float.
Returns the total market value of corporate bond positions in the given currency.
warrantsmarketvalue:
float.
Returns the total market value of warrant positions in the given currency.
netliquidationvalue:
float.
Returns the current net liquidation value of the positions held in the given currency.
interest:
float.
Returns the margin interest rate on the given currency.
unrealizedpnl:
float.
Returns the unrealized profit and loss for positions in the given currency.
stockmarketvalue:
float.
Returns the total market value of stock positions in the given currency.
moneyfunds:
float.
Returns the total market value of money funds positions in the given currency.
currency:
String.
Returns the currency’s symbol.
realizedpnl:
float.
Returns the realized profit and loss for positions in the given currency.
funds:
float.
Returns the total market value of all funds positions in the given currency.
acctcode:
String.
Returns the account ID for the account owner specified.
issueroptionsmarketvalue:
float.
Returns the total market value of all issuer option positions in the given currency.
key:
String.
Returns “LedgerList”. Internal use only.
timestamp:
int.
Returns the timestamp for the value retrieved in epoch time.
severity:
int.
Internal use only.
stockoptionmarketvalue:
float.
Returns the total market value of all stock option positions in the given currency.
futuresonlypnl:
float.
tbondsmarketvalue:
float.
Returns the total market value of all treasury bond positions in the given currency.
futureoptionmarketvalue:
float.
Returns the total market value of all futures option positions in the given currency.
cashbalancefxsegment:
float.
Internal use only.
secondkey:
String.
Returns the currency’s symbol.
tbillsmarketvalue:
float.
Returns the total market value of all treasury bill positions in the given currency.
dividends:
float.
Returns the value of dividends held from the given currency.
}
{
  "USD": {
    "commoditymarketvalue": 0.0,
    "futuremarketvalue": -1051.0,
    "settledcash": 214716688.0,
    "exchangerate": 1,
    "sessionid": 1,
    "cashbalance": 214716688.0,
    "corporatebondsmarketvalue": 0.0,
    "warrantsmarketvalue": 0.0,
    "netliquidationvalue": 215335840.0,
    "interest": 305569.94,
    "unrealizedpnl": 39695.82,
    "stockmarketvalue": 314123.88,
    "moneyfunds": 0.0,
    "currency": "USD",
    "realizedpnl": 0.0,
    "funds": 0.0,
    "acctcode": "U1234567",
    "issueroptionsmarketvalue": 0.0,
    "key": "LedgerList",
    "timestamp": 1702582321,
    "severity": 0,
    "stockoptionmarketvalue": -2.88,
    "futuresonlypnl": -1051.0,
    "tbondsmarketvalue": 0.0,
    "futureoptionmarketvalue": 0.0,
    "cashbalancefxsegment": 0.0,
    "secondkey": "USD",
    "tbillsmarketvalue": 0.0,
    "endofbundle": 1,
    "dividends": 0.0
  },
  "BASE": {
    "commoditymarketvalue": 0.0,
    "futuremarketvalue": -1051.0,
    "settledcash": 215100080.0,
    "exchangerate": 1,
    "sessionid": 1,
    "cashbalance": 215100080.0,
    "corporatebondsmarketvalue": 0.0,
    "warrantsmarketvalue": 0.0,
    "netliquidationvalue": 215721776.0,
    "interest": 305866.88,
    "unrealizedpnl": 39907.37,
    "stockmarketvalue": 316365.38,
    "moneyfunds": 0.0,
    "currency": "BASE",
    "realizedpnl": 0.0,
    "funds": 0.0,
    "acctcode": "U1234567",
    "issueroptionsmarketvalue": 0.0,
    "key": "LedgerList",
    "timestamp": 1702582321,
    "severity": 0,
    "stockoptionmarketvalue": -2.88,
    "futuresonlypnl": -1051.0,
    "tbondsmarketvalue": 0.0,
    "futureoptionmarketvalue": 0.0,
    "cashbalancefxsegment": 0.0,
    "secondkey": "BASE",
    "tbillsmarketvalue": 0.0,
    "dividends": 0.0
  }
}
Position & Contract Info
Copy Location
Returns an object containing information about a given position along with its contract details.
GET /portfolio/positions/{conid}
Request Object
Path Params
conId:
String.
Required
The contract ID to receive position information on.
Python
cURL
request_url = f"{baseUrl}/portfolio/positions/265598"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/portfolio/positions/265598 \
--request GET
Response Object
accountId:
String.
Displays the accountId returning data for.
acctId:
String.
Displays the accountId to return data for.
conid:
int.
Returns the contract ID of the position.
contractDesc:
String.
Returns the local symbol of the order.
position:
float.
Returns the total size of the position.
mktPrice:
float.
Returns the current market price of each share.
mktValue:
float.
Returns the total value of the order.
avgCost:
float.
Returns the average cost of each share in the position times the multiplier.
avgPrice:
float.
Returns the average cost of each share in the position when purchased.
realizedPnl:
float.
Returns the total profit made today through trades.
unrealizedPnl:
float.
Returns the total potential profit if you were to trade.
exchs:
null.
Deprecated value.
Always returns null.
currency:
String.
Returns the traded currency for the contract.
time:
int.
Returns amount of time in ms to generate the data.
chineseName:
String.
Returns the Chinese characters for the symbol.
allExchanges:
String*.
Returns a series of exchanges the given symbol can trade on.
listingExchange:
String.
Returns the primary or listing exchange the contract is hosted on.
countryCode:
String.
Returns the country code the contract is traded on.
name:
String.
Returns the comapny name.
assetClass:
String.
Returns the asset class or security type of the contract.
expiry:
String.
Returns the expiry of the contract. Returns null for non-expiry instruments.
lastTradingDay:
String.
Returns the last trading day of the contract.
group:
String.
Returns the group or industry the contract is affilated with.
putOrCall:
String.
Returns if the contract is a Put or Call option.
sector:
String.
Returns the contract’s sector.
sectorGroup:
String.
Returns the sector’s group.
strike:
int.
Returns the strike of the contract.
ticker:
String.
Returns the ticker symbol of the traded contract.
undConid:
int.
Returns the contract’s underlyer.
multiplier:
float,
Returns the contract multiplier.
type:
String.
Returns stock type.
hasOptions:
bool.
Returns if contract has tradable options contracts.
fullName:
String.
Returns symbol name for requested contract.
isUS:
bool.
Returns if the contract is US based or not.
incrementRules:
Array.
Returns rules regarding incrementation for market data and order placemnet.
lowerEdge:
float,
Returns lower edge value used to calculate increment.
increment:
float.
Allowed incrementable value.
displayRule:
object.
Returns an object containing display content for market data.
magnification:
int.
Returns maginification or multiplier of contract
displayRuleStep:
Array.
Contains various rules in the display object.
decimalDigits:
int.
Retrns average decimal digit for data display.
lowerEdge:
float.
Returns lower edge value used to calculate increment.
wholeDigits:
int.
Returns allowed display size.
isEventContract:
bool.
Returns if the contract is an event contract or not.
pageSize:
int.
Returns the content size of the request.
}]
[
  {
    "acctId": "U1234567",
    "conid": 265598,
    "contractDesc": "AAPL",
    "position": 614.2639,
    "mktPrice": 197.7639923,
    "mktValue": 121479.28,
    "currency": "USD",
    "avgCost": 192.7477563,
    "avgPrice": 192.7477563,
    "realizedPnl": 0.0,
    "unrealizedPnl": 3081.29,
    "exchs": null,
    "expiry": null,
    "putOrCall": null,
    "multiplier": null,
    "strike": 0.0,
    "exerciseStyle": null,
    "conExchMap": [],
    "assetClass": "STK",
    "undConid": 0,
    "model": ""
  }
]
PortfolioAnalyst
Copy Location
All Periods
Copy Location
Returns the performance across all available time periods for the given accounts, if more than one account is passed, the result is consolidated.
POST /pa/allperiods
Request Object
Body Parameters
acctIds:
Array of Strings.
Required
Include each account ID to receive data for.
Python
cURL
request_url = f"{baseUrl}/pa/performance"
json_content = {
  "acctIds": ["U1234567"]
}
requests.post(url=request_url, json=json_content)
curl \
{{baseUrl}}/pa/allperiods\
--request POST \
--header 'Content-Type:application/json' \
--data '{
  "acctIds": ["U1234567", "U4567890"]}'
Response Object
currencyType:
String.
Confirms the currency type.
If trading exclusively in your base currency, “base” will be returned.
rc:
int.
Returns the data identifier (Client Portal Only).
view:
Array of Strings.
Returns the accounts included in the response.
nd:
int.
Returns the total data points.
id:
String.
Returns the request identifier.
Internal use only.
included:
Array of Strings.
Returns the accounts included in the response.
pm:
String.
Portfolio Measure. Used to indicate TWR or MWR values returned.
{AccountID}:
Object.
Returns the account identifier for the referenced object.
{Period Value}:
Object.
Designates the period the data covers.
Potential Values: “1D”,”7D”,”MTD”,”1M”,”YTD”,”1Y”
{
nav:
Object.
Net asset value data for the account or consolidated accounts. NAV data is not applicable to benchmarks.
cps:
Array of Integers.
Returns an array containing Cumulative performance data over the period.
freq:
String.
Displays the values corresponding to a given frequency.
dates:
Array of Strings.
Returns the array of dates corresponding to your frequency, the length should be same as the length of returns inside data.
startNAV:
Object.
Returns the intiial NAV available.
{
date:
String.
Returns the starting date for the request.
val:
int.
Returns the Net Asset Value of the account.
}
}
periods:
String.
Returns the period ranges included in the response.
start:
String.
Returns the starting value of the value range.
end:
String.
Returns the end of the available frequency.
baseCurrency:
String.
Returns the base currency used in the account.
{
    "currencyType": "base",
    "rc": 0,
    "view": [
        "U1234567"
    ],
    "nd": 366,
    "id": "getPerformanceAllPeriods",
    "included": [
        "U1234567"
    ],
    "pm": "TWR",
    "U1234567": {
        "1D": {
            "nav": [
                3666392.5393
            ],
            "cps": [
                0.0005
            ],
            "freq": "D",
            "dates": [
                "20250603"
            ],
            "startNAV": {
                "date": "20250602",
                "val": 3664681.7504
            }
        },
        "lastSuccessfulUpdate": "2025-06-03 15:22:03",
        "start": "20240603",
        "YTD": {
            "nav": [
                3674381.3273,
                ...,
                3666392.5393
            ],
            "cps": [
                0,
                -0.0061,
                ...,
                -0.0021
            ],
            "freq": "D",
            "dates": [
                "20250101",
                ...,
                "20250603"
            ],
            "startNAV": {
                "date": "20241231",
                "val": 3674236.8245
            }
        },
        "1Y": {
            "nav": [
                3072764.5772,
                ...,
                3666392.5393
            ],
            "cps": [
                0.0054,
                ...,
                0.1996
            ],
            "freq": "D",
            "dates": [
                "20240603",
                ...,
                "20250603"
            ],
            "startNAV": {
                "date": "20240531",
                "val": 3056403.4525
            }
        },
        "periods": [
            "1D",
            "7D",
            "MTD",
            "1M",
            "YTD",
            "1Y"
        ],
        "end": "20250603",
        "MTD": {
            "nav": [
                3664681.7504,
                3666392.5393
            ],
            "cps": [
                0.003,
                0.0035
            ],
            "freq": "D",
            "dates": [
                "20250602",
                "20250603"
            ],
            "startNAV": {
                "date": "20250530",
                "val": 3653634.7799
            }
        },
        "1M": {
            "nav": [
                3626879.8271,
                ...,
                3666392.5393
            ],
            "cps": [
                -0.0046,
                ...,
                0.0063
            ],
            "freq": "D",
            "dates": [
                "20250505",
                ...,
                "20250603"
            ],
            "startNAV": {
                "date": "20250502",
                "val": 3643556.8781
            }
        },
        "7D": {
            "nav": [
                3649592.4093,
                ...,
                3666392.5393
            ],
            "cps": [
                -0.0005,
                ...,
                0.0041
            ],
            "freq": "D",
            "dates": [
                "20250528",
                ...,
                "20250603"
            ],
            "startNAV": {
                "date": "20250527",
                "val": 3651501.5873
            }
        },
        "baseCurrency": "USD"
    }
}
Account Performance
Copy Location
Returns the performance (MTM) for the given accounts, if more than one account is passed, the result is consolidated.
POST /pa/performance
Request Object
Body Parameters
acctIds:
Array of Strings.
Required
Include each account ID to receive data for.
period:
String.
Required
Specify the period for which the account should be analyzed.
Available Values: “1D”,”7D”,”MTD”,”1M”,”YTD”,”1Y”
Python
cURL
request_url = f"{baseUrl}/pa/performance"
json_content = {
  "acctIds": ["U1234567"]
  "period": "1D"
}
requests.post(url=request_url, json=json_content)
curl \
{{baseUrl}}/pa/performance \
--request POST \
--header 'Content-Type:application/json' \
--data '{
  "acctIds": ["U1234567", "U4567890"]
  "period": "1D"
}'
Response Object
currencyType:
String.
Confirms if the currency type.
If trading primarily in your base currency, “base” will be returned.
rc:
int.
Returns the data identifier (Client Portal Only).
nav:
Object.
Net asset value data for the account or consolidated accounts. NAV data is not applicable to benchmarks.
data:
Array of Object.
Contains the affiliated ‘nav’ data.
idType:
String.
Returns how identifiers are determined.
navs:
int.
Returns the series of data points corresponding to the listed days.
start:
String.
Returns the first available date for data.
end:
String.
Returns the end of the available frequency.
id:
String.
Returns the account identifier.
startNAV:
Object.
Returns the intiial NAV available.
date:
String.
Returns the starting date for the request.
val:
int.
Returns the Net Asset Value of the account.
baseCurrency:
String.
Returns the base currency used in the account.
freq:
String.
Displays the values corresponding to a given frequency.
dates:
Array of Strings.
Returns the array of dates corresponding to your frequency, the length should be same as the length of returns inside data.
nd:
int.
Returns the total data points.
cps:
object.
Returns the object containing the Cumulative performance data.
data:
Array of Objects.
Returns the array of cps data available.
idType:
String.
Returns the key value of the request.
start:
String.
Returns the starting value of the value range.
end:
String.
Returns the ending value of the value range.
returns:
Array of ints.
Returns all cps values in order between the start and end times.
id:
String.
Returns the account identifier.
baseCurrency:
String.
Returns the base curency for the account.
freq:
String.
Returns the determining frequency of the data range.
dates:
Array of Strings.
Returns the dates corresponding to the frequency of data.
tpps:
Object.
Returns the Time period performance data.
data:
Array.
Object containing all data about tpps.
idType:
String.
Returns the key value of the request.
start:
String.
Returns the starting value of the value range.
end:
String.
Returns the ending value of the value range.
returns:
Array of ints.
Returns all cps values in order between the start and end times.
id:
String.
Returns the account identifier.
baseCurrency:
String.
Returns the base curency for the account.
freq:
String.
Returns the determining frequency of the data range.
dates:
Array of Strings.
Returns the dates corresponding to the frequency of data.
id:
String.
Returns the request identifier, getPerformanceData.
included:
Array.
Returns an array contianing accounts reviewed.
pm:
String.
Portfolio Measure. Used to indicate TWR or MWR values returned.
{
  "currencyType": "base",
  "rc": 0,
  "nav": {
    "data": [
      {
        "idType": "acctid",
        "navs": [
          2.027673321223E8,
          {...},
          2.157185988239E8
        ],
        "start": "20230102",
        "end": "20231213",
        "id": "U1234567",
        "startNAV": {
          "date": "20221230",
          "val": 2.027677613449E8
        },
        "baseCurrency": "USD"
      }
    ],
    "freq": "D",
    "dates": [
      "20230102",
          {...},
      "20231213"
    ]
  },
  "nd": 346,
  "cps": {
    "data": [
      {
        "idType": "acctid",
        "start": "20230102",
        "end": "20231213",
        "returns": [
          0,
          {...},
          0.0639
        ],
        "id": "U1234567",
        "baseCurrency": "USD"
      }
    ],
    "freq": "D",
    "dates": [
      "20230102",
          {...},
      "20231213"
    ]
  },
  "tpps": {
    "data": [
      {
        "idType": "acctid",
        "start": "20230102",
        "end": "20231213",
        "returns": [
          0.0037,
          0.0031,
          0.0033,
          0.0034,
          0.02,
          0.0127,
          0.0036,
          0.0036,
          0.0034,
          0.0012,
          0.0026,
          0.0017
        ],
        "id": "U1234567",
        "baseCurrency": "USD"
      }
    ],
    "freq": "M",
    "dates": [
      "202301",
      "202302",
      "202303",
      "202304",
      "202305",
      "202306",
      "202307",
      "202308",
      "202309",
      "202310",
      "202311",
      "202312"
    ]
  },
  "id": "getPerformanceData",
  "included": [
    "U1234567"
  ],
  "pm": "TWR"
}
Transaction History
Copy Location
Transaction history for a given number of conids and accounts.
Types of transactions include dividend payments, buy and sell transactions, transfers.
POST /pa/transactions
Request Object
Body Parameters
acctIds:
Array of Strings.
Required
Include each account ID to receive data for.
conids:
Array of integers.
Required
Include contract ID to receive data for.
Only supports one contract id at a time.
currency:
String.
Required
Define the currency to display price amounts with.
Defaults to USD.
days:
String.
Optional
Specify the number of days to receive transaction data for.
Defaults to 90 days of transaction history if unspecified.
Python
cURL
request_url = f"{baseUrl}/pa/transactions"
json_content = {
  "acctIds": [
    "U1234567"
  ],
  "conids": [
    265598
  ],
  "currency": "USD",
  "days": 3
}
requests.post(url=request_url, json=json_content)
curl \
--url {{baseUrl}}/pa/transactions\
--request POST \
--header 'Content-Type:application/json' \
--data '{
  "acctIds": [
    "U1234567"
  ],
  "conids": [
    265598
  ],
  "currency": "USD",
  "days": 3
}'
Response Object
rc:
int.
(Client portal use only)
nd:
int.
(Client portal use only)
rpnl:
Object.
Rturns the object containing the realized pnl for the contract on the date.
data:
Array of objects.
Returns an array of realized pnl objects.
date:
String.
Specifies the date for the transaction.
cur:
String.
Specifies the currency of the realized value.
fxRate:
int.
Returns the foreign exchnage rate.
side:
String.
Determines if the day was a loss or gain
Value format: “L”, “G”
acctid:
String.
Returns the account ID the trade transacted on.
amt:
String.
Returns the amount gained or lost on the day.
conid:
String.
Returns the contract ID of the transaction.
amt:
String.
Provides the total amount gained or lost from all days returned
currency:
String.
Returns the currency the account is traded in.
from:
int.
Returns the epoch time for the start of requests.
id:
String.
Returns the request identifier, getTransactions.
to:
int.
Returns the epoch time for the end of requests.
includesRealTime:
bool.
Returns if the trades are up to date or not.
transactions:
Array of objects.
Lists all supported transaction values.
date:
String.
Reutrns the human-readable datetime of the transaction.
Value Format: “{Day of the week} {3-digit month} {day of the month} 00:00:00 {timezone} {year}”
cur:
String.
Returns the currency of the traded insturment.
fxRate:
int.
Returns the forex conversion rate.
pr:
float.
Returns the price per share of the transaction.
qty:
int.
Returns the total quantity traded.
Will display a negative value for sell orders, and a positive value for buy orders.
acctid:
String.
Returns the account which made the transaction.
amt:
float.
Returns the total value of the trade.
conid:
int.
Returns the contract identifier.
type:
String.
Returns the order side.
desc:
String.
Returns the long name for the company.
{
  "rc": 0,
  "nd": 4,
  "rpnl": {
    "data": [
      {
        "date": "20231211",
        "cur": "USD",
        "fxRate": 1,
        "side": "L",
        "acctid": "U1234567",
        "amt": "12.2516",
        "conid": "265598"
      }
    ],
    "amt": "12.2516"
  },
  "currency": "USD",
  "from": 1702270800000,
  "id": "getTransactions",
  "to": 1702530000000,
  "includesRealTime": true,
  "transactions": [
    {
      "date": "Mon Dec 11 00:00:00 EST 2023",
      "cur": "USD",
      "fxRate": 1,
      "pr": 192.26,
      "qty": -5,
      "acctid": "U1234567",
      "amt": 961.3,
      "conid": 265598,
      "type": "Sell",
      "desc": "Apple Inc"
    }
  ]
}
Scanner
Copy Location
Iserver Scanner Parameters
Copy Location
Returns an xml file containing all available parameters to be sent for the Iserver scanner request.
GET /iserver/scanner/params
Request Object
No parameters or body content should be sent.
Python
cURL
request_url = f"{baseUrl}/iserver/scanner/params"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/iserver/scanner/params \
--request GET
Response Object
scan_type_list:
List Array of objects.
Contains all values used as the scanner “type” in the request.
[{
display_name:
String.
Human readable name for the scanner “type”
code:
String.
Value used for the market scanner request.
instruments:
Array of Strings.
Returns all instruments the scanner type can be used with.
}]
instrument_list:
Array of Objects.
Contains all values relevant to the scanner “instrument” request field.
[{
display_name:
String.
Human readable representation of the instrument type.
type:
String.
Value used for the market scanner request.
filters:
Array of Strings.
Returns an array of all filters uniquely avaliable to that instrument type.
}]
filter_list:
Array of Objects.
[{
group:
String.
Returns the group of filters the request is affiliated with.
display_name:
String.
Returns the human-readable identifier for the filter.
code:
String.
Value used for the market scanner request.
type:
String.
Returns the type of value to be used in the request.
This can indicate a range based value, or if it should be a single value.
}]
location_tree:
Array of objects.
Contains all values relevant to the location field of the market scanner request.
display_name:
String.
Returns the overarching instrument type to designate the location.
type:
String.
Returns the code value of the market scanner instrument type value.
locations:
Array of objects.
[{
display_name:
String.
Returns the human-readable value of the market scanner’s location value.
type:
String.
Returns the code value of the market scanner location value.
locations:
Array.
Always returns an empty array at this depth.
}]
]
{
  "scan_type_list":[
    {
      "display_name": "display_name",
      "code": "code",
      "instruments": []
    }
  ],
  "instrument_list":[ 
    {
      "display_name": "display_name",
      "type": "type",
      "filters": []
    }
  ],
  "filter_list":[
    {
      "group": "group",
      "display_name": "display_name",
      "code": "code",
      "type": "type"
    }
  ],
  "location_tree":[
    {
      "display_name": "display_name",
      "type": "type",
      "locations": [
        {
          "display_name": "display_name",
          "type": "type",
          "locations": []
        }
      ]
    }
  ]
}
Iserver Market Scanner
Copy Location
Searches for contracts according to the filters specified in /iserver/scanner/params endpoint
Users can receive a maximum of 50 contracts from 1 request.
POST /iserver/scanner/run
Request Object
Body Parameters
instrument:
String.
Required
Instrument type as the target of the market scanner request.
Found in the “instrument_list” section of the /iserver/scanner/params response.
type:
String.
Required
Scanner value the market scanner is sorted by.
Based on the “scan_type_list” section of the /iserver/scanner/params response.
location:
String.
Required
Location value the market scanner is searching through.
Based on the “location_tree” section of the /iserver/scanner/params response.
filter:
Array of objects.
Contains any additional filters that should apply to response.
[{
code:
String.
Code value of the filter.
Based on the “code” value within the “filter_list” section of the /iserver/scanner/params response.
value:
int.
Value corresponding to the input for “code”.
}]
Python
cURL
request_url = f"{baseUrl}/iserver/scanner/run"
json_content = {
  "instrument": "STK",
  "location": "STK.US.MAJOR",
  "type": "TOP_TRADE_COUNT",
  "filter": [
    {
      "code":"priceAbove",
      "value":5
    }
  ]
}
requests.post(url=request_url, json=json_content)
curl \
--url {{baseUrl/iserver/scanner/run \
--request POST \
--header 'Content-Type:application/json' \
--data '{
  "instrument": "STK",
  "location": "STK.US.MAJOR",
  "type": "TOP_PERC_GAIN",
  "filter": [
    {
      "code":"priceAbove",
      "value":5
    }
  ]
}'
Response Object
contracts:
Array of objects.
Contains contracts related to the market scanner request.
[{
server_id:
String.
Contract’s index in relation to the market scanner type’s sorting priority.
column_name:
String.
Always returned for the first contract.
Used for Client Portal (Internal use only)
symbol:
String.
Returns the contract’s ticker symbol.
conidex:
String.
Returns the contract ID of the contract.
con_id:
int.
Returns the contract ID of the contract.
available_chart_periods:
String.
Used for Client Portal (Internal use only)
company_name:
String.
Returns the company long name.
contract_description_1:
String.
For derivatives like Futures, the local symbol of the contract will be returned.
listing_exchange:
String.
Returns the primary listing exchange of the contract.
sec_type:
String.
Returns the security type of the contract.
}],
scan_data_column_name:
String.
Used for Client Portal (Internal use only)
{
  "contracts": [
    {
      "server_id": "0",
      "symbol": "AMD",
      "conidex": "4391",
      "con_id": 4391,
      "available_chart_periods": "#R|1",
      "company_name": "ADVANCED MICRO DEVICES",
      "scan_data": "163.773K",
      "contract_description_1": "AMD",
      "listing_exchange": "NASDAQ.NMS",
      "sec_type": "STK"
    }
  ],
  "scan_data_column_name": "Trades"
}
HMDS Market Scanner
Copy Location
Request a market scanner from our HMDS service.
Can return a maximum of 250 contracts.
Developers should first call the
/hmds/auth/init
endpoint prior to their request to avoid an initial 404 rejection.
POST /hmds/scanner
Request Object
Body Parameters
instrument:
String.
Required
Specify the type of instrument for the request.
Found under the “instrument_list” value of the /iserver/scanner/params request.
locations:
String.
Required
Specify the type of location for the request.
Found under the “location_tree” value of the /iserver/scanner/params request.
scanCode:
String.
Required
Specify the scanner type for the request.
Found under the “scan_type_list” value of the /iserver/scanner/params request.
secType:
String.
Required
Specify the type of security type for the request.
Found under the “location_tree” value of the /iserver/scanner/params request.
delayedLocations:
null.
Internal use only.
maxItems:
int.
Specify how many items should be returned.
Default and maximum set to 250.
filters:
Array of object.
Required*
Array of objects containing all filters upon the scanner request.
Content contains a series of key:value pairs.
While “filters” must be specified in the body, no content in the array needs to be passed.
Python
cURL
request_url = f"{baseUrl}/hmds/scanner"
json_content= {
  "instrument":"BOND",
  "locations": "BOND.US",
  "scanCode": "HIGH_BOND_ASK_YIELD_ALL",
  "secType": "BOND",
  "delayedLocations":"SMART",
  "maxItems":25,
  "filters":[{
    "bondAskYieldBelow": 15.819
  }]
}
requests.post(url=request_url, json=json_content)
curl \
--url {{baseUrl}}/hmds/scanner \
--request POST \
--header 'Content-Type:application/json' \
--data '{
  "instrument":"BOND",
  "locations": "BOND.US",
  "scanCode": "HIGH_BOND_ASK_YIELD_ALL",
  "secType": "BOND",
  "delayedLocations":"SMART",
  "maxItems":25,
  "filters":[{
    "bondAskYieldBelow": 15.819
  }]
}'
Response Object
contracts:
Array of objects.
Contains all contracts in order from the scanner response.
[{
inScanTime:
String.
Returns the time at which the contract was scanned.
Always returned in UTC time as a string.
contractID:
String.
Returns the contract identifier of the scanned contract.
con_id:
String.
Returns the contract identifier of the scanned contract.
}]
{
  "total": "17262",
  "size": "250",
  "offset": "0",
  "scanTime": "20231214-18:55:25",
  "id": "scanner1",
  "position": "v1:AAAAAQABG3gAAAAAAAAA+g==",
  "Contracts": {
    "Contract": [
      {
        "inScanTime": "20231214-18:55:25",
        "contractID": "431424315"
      },
  ]
}
Session
Copy Location
Requests used to designate changes to the web session itself rather than endpoints relating to trades or account data directly.
Authentication Status
Copy Location
Current Authentication status to the Brokerage system. Market Data and Trading is not possible if not authenticated, e.g. authenticated shows false
POST /iserver/auth/status
Request Object
No params or body content should be sent.
Python
cURL
request_url = f"{baseUrl}/iserver/auth/status"
json_content = {}
requests.post(url=request_url, json=json_content)
curl \
--url {{baseUrl}}/iserver/auth/status \
--request POST \
--header 'Content-Type:application/json' \
--data '{}'
Response Object
authenticated:
bool.
Returns whether your brokerage session is authenticated or not.
competing:
bool.
Returns whether you have a competing brokerage session in another connection.
connected:
bool.
Returns whether you are connected to the gateway, authenticated or not.
message:
String.
If there is a message about your authenticate status, it will be returned here.
Authenticated sessions return an empty string.
MAC:
String.
IBKR MAC information. Internal use only.
serverInfo:
Object.
serverName:
String.
IBKR server information. Internal use only.
serverVersion:
String.
IBKR version information. Internal use only.
hardware_info:
String.
IBKR version information. Internal use only.
fail:
String.
Returns the reason for failing to retrieve authentication status.
{
  "authenticated": true,
  "competing": false,
  "connected": true,
  "message": "",
  "MAC": "12:B:B3:23:BF:A0",
  "serverInfo": {
    "serverName": "JifN19053",
    "serverVersion": "Build 10.25.0p, Dec 5, 2023 5:48:12 PM"
  },
  "hardware_info": "3b0679ee|98:A2:B3:23:BC:A0",
  "fail": ""
}
Alternate Response Object
Users that have been timed out or logged out of their session will result in a “false” authentication status, indicating the user is not maintaining a brokerage session.
{
  "authenticated": false,
  "competing": false,
  "connected": false,
  "MAC": "98:B2:C3:45:DE:F6"
}
Initialize Brokerage Session
Copy Location
This is essential for using all endpoints besides /portfolio, including access to trading and market data.
POST /iserver/auth/ssodh/init
Request Object
Body Params
publish:
Boolean.
Required
Determines if the request should be sent immediately.
Users should always pass true. Otherwise, a ‘500’ response will be returned.
compete:
Boolean.
Required
Determines if other brokerage sessions should be disconnected to prioritize this connection.
Python
cURL
request_url = "{baseUrl}/iserver/auth/ssodh/init"
json_content= {"publish":True,"compete":True}
requests.post(url=request_url, json=json_content)
curl \
--url {{baseUrl}}/iserver/auth/ssodh/init \
--request POST \
--header 'Content-Type:application/json' \
--data '{
  "publish":true,
  "compete":true
}'
Response Object
authenticated:
bool.
Returns whether your brokerage session is authenticated or not.
competing:
bool.
Returns whether you have a competing brokerage session in another connection.
connected:
bool.
Returns whether you are connected to the gateway, authenticated or not.
message:
String.
If there is a message about your authenticate status, it will be returned here.
Authenticated sessions return an empty string.
MAC:
String.
IBKR MAC information. Internal use only.
serverInfo:
Object.
serverName:
String.
IBKR server information. Internal use only.
serverVersion:
String.
IBKR version information. Internal use only.
{
  "authenticated": true,
  "competing": false,
  "connected": true,
  "message": "",
  "MAC": "98:F2:B3:23:BF:A0",
  "serverInfo": {
    "serverName": "JifN19053",
    "serverVersion": "Build 10.25.0p, Dec 5, 2023 5:48:12 PM"
  }
}
Logout of the current session
Copy Location
Logs the user out of the gateway session. Any further activity requires re-authentication.
POST /logout
Request Object
No params or body content should be sent.
Python
cURL
request_url = "{baseUrl}/logout"
json_content= {}
requests.post(url=request_url, json=json_content)
curl \
--url {{baseUrl}}/logout \
--request POST \
--header 'Content-Type:application/json' \
--data '{}'
Response Object
status:
bool.
Returns true if the session was ended.
{
  "status":true
}
Ping the server
Copy Location
If the gateway has not received any requests for several minutes an open session will automatically timeout. The tickle endpoint pings the server to prevent the session from ending. It is expected to call this endpoint approximately every 60 seconds to maintain the connection to the brokerage session.
POST /tickle
Request Object
No params or body content should be sent.
Python
cURL
request_url = f"{baseUrl}/tickle"
json_content = {}
requests.post(url=request_url, json=json_content)
curl \ 
--url {{baseUrl}}/tickle \
--request POST \
--header 'Content-Type:application/json' \
--data '{}'
Response Object
session:
String.
Returns the session identifier of your connection.
Can be used for the cookie parameter of your request.
ssoExpires:
int.
Displays the time until session expiry in milliseconds.
collission:
bool.
Internal use only.
userId:
int.
Internal use only.
hmds:
object.
Returns any potential historical data-specific information.
“No bridge” indicates historical data is not being currently requested.
iserver:
object.
Returns the content of the
/iserver/auth/status
endpoint.
{
  "session": "bb665d0f55b6289d70bc7380089fc96f",
  "ssoExpires": 460311,
  "collission": false,
  "userId": 123456789,
  "hmds": {
    "error": "no bridge"
  },
  "iserver": {
    "authStatus": {
      "authenticated": true,
      "competing": false,
      "connected": true,
      "message": "",
      "MAC": "98:F2:B3:23:BF:A0",
      "serverInfo": {
        "serverName": "JifN19053",
        "serverVersion": "Build 10.25.0p, Dec 5, 2023 5:48:12 PM"
      }
    }
  }
}
Re-authenticate the Brokerage Session (Deprecated)
Copy Location
When using the CP Gateway, this endpoint provides a way to reauthenticate to the Brokerage system as long as there is a valid brokerage session.
All interest in reauthenticating the gateway session should be handled using the
/iserver/auth/ssodh/init
endpoint.
POST /iserver/reauthenticate
Request Object
No params or body content should be sent.
Python
cURL
request_url = f"{baseUrl}/iserver/reauthenticate"
json_content = {}
requests.post(url=request_url, json=json_content )
curl \
--url {{baseUrl}}/iserver/reauthenticate \ 
--request POST \ 
--header 'Content-Type:application/json' \ 
--data '{}'
Response Object
message:
String.
Returns “triggered” to indicate the response was sent.
{
  "message": "triggered"
}
Validate SSO
Copy Location
Validates the current session for the SSO user.
This endpoint is only valid for Client Portal Gateway and OAuth 2.0 clients.
GET /sso/validate
Request Object:
No additional parameters necessary.
Python
cURL
request_url = f"{baseUrl}/sso/validate"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/sso/validate \
--request GET
Response Object:
USER_ID:
int.
Internal user identifier.
USER_NAME:
String.
current username logged in for the session.
RESULT:
bool.
Confirms if validation was successful.
true if session was validated; false if not.
AUTH_TIME:
int.
Returns the time of authentication in epoch time.
SF_ENABLED:
bool.
Internal use only.
IS_FREE_TRIAL:
bool.
Returns if the account is a trial account or a funded account.
CREDENTIAL:
String.
Returns the underlying username of the account.
IP:
String.
Internal use only.
Does not reflect the IP address of the user.
EXPIRES:
int.
Returns the time until expiration in milliseconds.
QUALIFIED_FOR_MOBILE_AUTH:
bool.
Returns if the customer requires two factor authentication.
LANDING_APP:
String.
Used for Client Portal (Internal use only)
IS_MASTER:
bool.
Returns whether the account is a master account (true) or subaccount (false).
lastAccessed:
int.
Returns the last time the user was accessed in epoch time.
loginType:
int.
Returns the login type.
1 for Live, 2 for Paper
PAPER_USER_NAME:
Returns the paper username for the account.
features:
object.
Returns supported features such as bonds and option trading.
{
  "USER_ID": 123456789,
  "USER_NAME": "user1234",
  "RESULT": true,
  "AUTH_TIME": 1702580846836,
  "SF_ENABLED": false,
  "IS_FREE_TRIAL": false,
  "CREDENTIAL": "user1234",
  "IP": "12.345.678.901",
  "EXPIRES": 415890,
  "QUALIFIED_FOR_MOBILE_AUTH": null,
  "LANDING_APP": "UNIVERSAL",
  "IS_MASTER": false,
  "lastAccessed": 1702581069652,
  "LOGIN_TYPE": 2,
  "PAPER_USER_NAME": "user1234",
  "features": {
    "env": "PROD",
    "wlms": true,
    "realtime": true,
    "bond": true,
    "optionChains": true,
    "calendar": true,
    "newMf": true
  },
  "region": "NJ"
}
Watchlists
Copy Location
Manage watchlists that are used in both Trader Workstation and Client Portal.
Can also be used to maintain lists within the Client Portal API.
Create a Watchlist
Copy Location
Create a watchlist to monitor a series of contracts.
POST /iserver/watchlist
Request Object
Body Params
id:
String.
Required
Supply a unique identifier to track a given watchlist. Must supply a number.
name:
String.
Required
Supply the human readable name of a given watchlist. Displayed in TWS and Client Portal.
rows:
Array of Objects.
Required
[{
C:
int.
Provide the conid, or contract identifier, of the conid to add.
H:
Empty String.
Can be used to add a blank row between contracts in the watchlist.
}]
Python
cURL
request_url = f"{baseUrl}/iserver/watchlist"
json_content = {
  "id":"1234",
  "name":"Test Watchlist",
  "rows":[
    {"C":8314},
    {"C":8894}
  ]
}
requests.post(url=request_url, json=json_content)
curl \
--url {{baseUrl}}/iserver/watchlist \
--request POST \
--header 'Content-Type:application/json' \
--data '{
  "id":"1234",
  "name":"Test Watchlist",
  "rows":[
    {"C":8314},
    {"C":8894}
  ]
}'
Response Object
id:
String.
Returns the id value used to create the watchlist.
hash:
String.
Returns the internal IB hash value of the order.
name:
String.
Returns the human-readable name of the watchlist.
readOnly:
bool.
Determines if the watchlist is marked as write-restricted.
instruments:
Empty Array.
Always returns an empty array.
Conids supplied will still be in the final watchlist.
See the
/iserver/watchlist?id
endpoint for more details.
{
  "id": "1234",
  "hash": "1702581306241",
  "name": "Test Watchlist",
  "readOnly": false,
  "instruments": []
}
Get All Watchlists
Copy Location
Retrieve a list of all available watchlists for the account.
GET /iserver/watchlists
Request Object:
Body Params
SC:
String.
Specify the scope of the request.
Valid Values: USER_WATCHLIST
Python
cURL
request_url = f"{baseUrl}/iserver/watchlist?SC=USER_WATCHLIST"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/iserver/watchlists?SC=USER_WATCHLIST \
--request GET
Response Object
data:
Object.
Contains all of the data about the watchlist.
{
scanners_only:
bool.
Shows if the system is only displaying scanners.
system_lists:
Array of Objects.
Returns all IB-created watchlists.
[{
is_open:
bool.
Internal use only.
read_only:
bool.
Returns if the watchlist can be edited or not.
name:
String.
Returns the human-readable name of the watchlist.
id:
String.
Returns the code identifier of the watchlist.
type:
String.
Returns the watchlist type.
Always returns “watchlist”.
}],
show_scanners:
bool.
Returns if scanners are shown.
bulk_delete:
bool.
Displays if the watchlists should be deleted.
user_lists:
Array of Objects.
Returns all of the available user-created lists.
[{
is_open:
bool.
Internal use only.
read_only:
bool.
Returns if the watchlist can be edited or not.
name:
String.
Returns the human-readable name of the watchlist.
id:
String.
Returns the code identifier of the watchlist.
type:
String.
Returns the watchlist type.
Always returns “watchlist”.
}]
},
action:
String.
Internal use only.
Returns “content”.
MID:
String.
Returns the number of times the endpoint was requested this session.
}
{
  "data": {
    "scanners_only": false,
    "show_scanners": false,
    "bulk_delete": false,
    "user_lists": [
      {
        "is_open": false,
        "read_only": false,
        "name": "Test Watchlist",
        "modified": 1702581306241,
        "id": "1234",
        "type": "watchlist"
      }
    ]
  },
  "action": "content",
  "MID": "1"
}
Get Watchlist Information
Copy Location
Request the contracts listed in a particular watchlist.
GET /iserver/watchlist
Request Object
Query Params
id:
String.
Required
Set equal to the watchlist ID you would like data for.
Python
cURL
request_url = f"{baseUrl}/iserver/watchlist?id=1234"
requests.get(url=request_url)
curl \
--url {{baseUrl}}/iserver/watchlist?id=1234 \
--request GET
Response Object
The first request may only return the values C, conid, and name values. Subsequent requests will add additional contract information.
id:
String.
hash:
String.
name:
String.
readOnly:
bool.
instruments:
Array of Objects.
[{
C:
String.
Returns the contract ID.
conid:
int.
Returns the contract ID.
name:
String.
Returns the long name of the company.
fullName:
String.
Returns the local symbol of the contract.
assetClass:
String.
Returns the security type of the contract.
ticker:
String.
Returns the ticker symbol for the contract.
chineseName:
String.
Returns the Chinese character name for the contract.
}]
{
  "id": "1234",
  "hash": "1702581306241",
  "name": "Test Watchlist",
  "readOnly": false,
  "instruments": [
    {
      "ST": "STK",
      "C": "8314",
      "conid": 8314,
      "name": "INTL BUSINESS MACHINES CORP",
      "fullName": "IBM",
      "assetClass": "STK",
      "ticker": "IBM",
      "chineseName": "国际商业机器"
    },
    {
      "ST": "STK",
      "C": "8894",
      "conid": 8894,
      "name": "COCA-COLA CO/THE",
      "fullName": "KO",
      "assetClass": "STK",
      "ticker": "KO",
      "chineseName": "可口可乐"
    }
  ]
}
Delete a Watchlist
Copy Location
Permanently delete a specific watchlist for all platforms.
DELETE /iserver/watchlist
Request Object
id:
String.
Required
Include the watchlist ID you wish to delete.
Python
cURL
request_url = f"{baseUrl}/iserver/watchlist?id=1234"
requests.delete(url=request_url)
curl \
--url {{baseUrl}}/iserver/watchlist?id=1234 \
--request DELETE
Response Object
Data:
Object.
Returns the data about the deleted watchlist.
deleted:
String.
Returns the ID for the deleted watchlist.
action:
String.
Always returns “context”.
MID:
String.
Returns the id for the number of times /iserver/watchlist was called this session.
{
  "data": {
    "deleted": "1234"
  },
  "action": "context",
  "MID": "2"
}
Websockets
Copy Location
Websocket topics expose the same underlying data as is delivered by the HTTP endpoints. Functionality that requires a brokerage session (that is, all features behind /iserver URIs) will also require a brokerage session when accessed via websocket. Please ensure you have an active brokerage session before attempting to use these features of the websocket. For information on getting started with Client Portal API, please refer to the
Authentication
section.
Websocket topics requiring a brokerage session: smd (live market data), smh (historical market data), sbd (live price ladder data), sor (order updates), str (trades), act (unsolicited account property info), sts (unsolicited brokerage session authentication status), blt (unsolicited bulletins), ntf (unsolicited notifications).
Websocket topics that do not require a brokerage session: spl (profit & loss updates), ssd (account summary updates), sld (account ledger updates), system (unsolicited connection-related messages).
The url for websockets is:
wss://localhost:5000/v1/api/ws
Connection Guide
Copy Location
If you require brokerage functionality, you will need to establish a brokerage session prior to opening a websocket, just as is required before making requests to /iserver endpoints. Please see
Authentication
for more details.
Request Session Information
Copy Location
First make request the
/tickle
endpoint and save the returned session value.
{
    "session": "d21b8cf5ebc8ea01c6ce37c8125ec83f",
    "ssoExpires": ssoExpires,
    "collission": collission,
    "userId": userId,
    "hmds": {
        "error": "no bridge"
    },
    "iserver": {
        "authStatus": {
            "authenticated": true,
            "competing": false,
            "connected": true,
            "message": "",
            "MAC": "MAC",
            "serverInfo": {
                "serverName": "serverName",
                "serverVersion": "serverVersion"
            }
        }
    }
}
Retrieve the Session Token
Copy Location
We are then specifically interested in the “session” value in the response as we can pass this to our websocket for confirming our sessionId.
sessionToken = '{"session":"d21b8cf5ebc8ea01c6ce37c8125ec83f"}'
Establishing the Websocket with Client Portal Gateway
Copy Location
Next, you will need to build your websocket to wss://localhost:5000/v1/api/ws. In your request to establish the websocket, be sure to set your cookie header as “api={‘session’ value here}”
Python
cURL
ws = websocket.WebSocketApp(
  url="wss://localhost:5000/v1/api/ws",
  on_open=on_open,
  on_message=on_message,
  on_error=on_error,
  on_close=on_close,
  cookie=f"api={sessionToken}"
)
ws.run_forever()
curl -i -k -N -H "Connection: Upgrade" -H "Upgrade: websocket" -H "origin: interactivebrokers.github.io" --cookie "api=d21b8cf5ebc8ea01c6ce37c8125ec83f" wss://localhost:5000/v1/api/ws
Establishing the Websocket with OAuth
Copy Location
The process for those authenticating with OAuth is similar, though slightly different. In addition to the API cookie, you must also include the “oauth_token” query param which should be set to the user’s access token value.
Python
cURL
ws = websocket.WebSocketApp(
  url="wss://api.ibkr.com/v1/api/ws?oauth_token={accessToken}",
  on_open=on_open,
  on_message=on_message,
  on_error=on_error,
  on_close=on_close,
  cookie=f"api={sessionToken}"
)
ws.run_forever()
curl -i -N -H "Connection: Upgrade" -H "Upgrade: websocket" -H "origin: interactivebrokers.github.io" --cookie "api=d21b8cf5ebc8ea01c6ce37c8125ec83f" wss://api.ibkr.com/v1/api/ws?oauth_token={Access Token}
Send a Websocket Topic
Copy Location
After establishing your session, you may send whichever topics are needed through the newly established websocket.
Python
cURL
on_open(ws):
    print("Opened Connection")
    time.sleep(3)
    ws.send('smd+265598+{"fields":["31","84","86"]}')
Please note that while the websocket session itself supports the ability to establish a websocket, cURL is unable to send future topic requests. This would need to be facilitated by either a third party terminal add-on, or a programming language such as Python, Java, or otherwise.
Solicited and Unsolicited Messages
Copy Location
There are two types of messages sent to clients via the websocket:
Solicited messages:
Messages delivered to the client in response to an explicit, client-initiated subscription to a topic.
Unsolicited messages:
Messages delivered automatically to the client by the server. These messages are not associated with a topic subscription that can be canceled, and they typically contain session or other administrative information.
Subscribing to Websocket Topics
Copy Location
Most data available via the websocket is delivered as a stream of messages in response to an explicit subscription to a topic. Such subscription messages are solicited, because the client must first ask to be subscribed to the relevant topic before messages will be sent by the server. To subscribe to a topic, a message is sent to the websocket in the following format:
TOPIC+[TOPIC_TARGET]+{PARAMETERS}
where:
TOPIC
is the identifier of the topic (the type of data) to be subscribed.
The plus symbol
+
is used as a separator of the message elements.
TOPIC_TARGET
identifies a specific resource, typically an account or instrument, as the subject of the subscription. Certain topics do not use a target.
{PARAMETERS}
is a JSON-formatted string that describes filters or other modifiers to the topic subscription. If no parameters are available for the topic, or none are desired for your subscription, this is sent as an empty {} object.
Solicited message topics are generally three characters in length. A message sent to subscribe, as well as the messages received in response for the duration of the subscription, will use a topic starting with
s
(“subscribe”). The second two characters identify the particular datafeed in question, as in topic
ssd
, indicating “subscribe” + “account summary”.
When canceling a subscription (unsubscribing), a message is sent using a topic starting with
u
(“unsubscribe”), followed by the two-character identifier of the datafeed whose subscription will be terminated, as in topic
usd
, indicating “unsubscribe” + “account summary”. A single response message will be delivered with the same unsubscribe topic, confirming unsubscription.
Account Operations
Copy Location
Subscribe Account Summary
Copy Location
Subscribe to Account Summary Topic
Topic:
ssd
Subscribes to a stream of account summary messages for the specified account.
Topic Target:
accountId:
Required
.
Must pass the account ID whose account summary data will be subscribed.
Parameters:
{
keys:
Array of Strings.
Pass specific account summary data keys to receive messages concerning only those keys. Passing no named keys when opening the subscription will deliver account summary messages containing values for the selected account.
Example Values: “AccruedCash-S”, “ExcessLiquidity-S”
fields:
Array of Strings.
Pass specific account summary field names to filter responses to include only these fields for the requested keys. Passing no named fields when opening the subscription will deliver all available data points for the specified account summary keys.
Example Values: “currency”, “monetaryValue”
}
ssd+DU1234567+{
    "keys":["AccruedCash-S","ExcessLiquidity-S"],
    "fields":["currency","monetaryValue"]
}
Account Summary Topic Messages
{
result:
Array of JSON objects, each corresponding to an account summary value for the account.
[
{
key:
String.
The name of the account summary value.
This is always returned.
timestamp:
Number (integer only).
The timestamp reflecting when the value was retrieved.
This is always returned.
value:
String.
A non-monetary value associated with the key. This may include dates, account titles, or other relevant information.
monetaryValue:
Number.
A monetary value associated with the key. Returned when the key pertains to pricing or balance details.
currency:
String.
The currency reflected by monetaryValue.
Example Value: “USD”, “EUR”, “HKD”
severity:
Number (integer only).
Internal use only.
},
…
]
}
{"result":[
    {
     "key":"key1",
     "currency":"currency", 
     "monetaryValue":monetaryValue, 
     "severity":0, 
     "timestamp":timestamp
    },
    {
     "key":"key2",
     "currency":"currency", 
     "value":value, 
     "severity":0, 
     "timestamp":timestamp
    },
]}
Unsubscribe Account Summary
Copy Location
Unsubscribe from Account Summary Topic
Topic:
usd
Unsubscribes the user from account summary information for the specified account.
Topic Target:
accountId:
Required
.
Must pass the account ID whose account summary messages will be unsubscribed.
Parameters:
none
usd+DU1234567+{}
Account Summary Unsubscribe Message
Arrives once.
{
result:
String.
Confirms successful unsubscription.
}
{"result":"unsubscribed from summary"}
Subscribe Account Ledger
Copy Location
Subscribe to Account Ledger Topic
Topic:
sld
Subscribes to a stream of account ledger messages for the specified account, with contents sorted by currency.
Topic Target:
accountId:
Required
.
Must pass the account ID whose ledger data will be subscribed.
Parameters:
{
keys:
Array of Strings.
Pass specific ledger currency keys to receive messages with data only for those currencies. Passing no named keys when opening the subscription will deliver ledger messages containing values for all currencies in the selected account.
Example Values: “LedgerListEUR”, “LedgerListUSD”, “LedgerListBASE” (for the account’s base currency)
fields:
Array of Strings.
Pass specific ledger field names to receive messages only those data points for the currencies specified in the keys argument. Passing no named fields when opening the subscription will deliver all available data points for the specified currencies.
Example Values: “cashBalance”, “exchangeRate”
}
sld+DU1234567+{
    "keys":["LedgerListBASE","LedgerListEUR"],
    "fields":["cashBalance","exchangeRate"]
}
Account Ledger Topic Messages
A new message is published every 10 seconds until the sld topic is unsubscribed. A given message will only deliver a currency’s field data when a change occurred for that currency in the preceding interval. If no change occurred, the currency’s entry in the sld message will be “blank”, containing only the currency key and a timestamp.
Note that all currency values of JSON number type will be presented with a fractional component following a decimal point, and may also include an exponential component following an E if sufficiently large.
{
result:
Array of JSON objects, with each object containing the set of key-value pairs for one currency in the account.
[
{
key:
String.
Currency identifier string in the form “LedgerListXXX”, where XXX is the three-character currency code of a currency in the requested account, or “LedgerListBASE”, corresponding to the account’s base currency.
This is always returned.
timestamp:
Number (integer only).
The timestamp reflecting when the currency’s set of values was retrieved.
This is always returned.
acctCode:
String.
The account containing the currency position described by the accompanying data.
cashbalance:
Number.
cashBalanceFXSegment:
Number.
commodityMarketValue:
Number.
corporateBondsMarketValue:
Number.
dividends:
Number.
exchangeRate:
Number.
funds:
Number.
marketValue:
Number.
optionMarketValue:
Number.
interest:
Number.
issueOptionsMarketValue:
Number.
moneyFunds:
Number.
netLiquidationValue:
Number.
realizedPnl:
Number.
unrealizedPnl:
Number.
secondKey:
String.
settledCash:
Number.
stockMarketValue:
Number.
tBillsMarketValue:
Number.
tBondsMarketValue:
Number.
warrantsMarketValue:
Number.
severity:
Number (integer only).
Internal use only.
},
…
]
}
{
  "result": [
    {
      "acctCode": "DU1234567",
      "cashbalance": 2.0201311791131118E8,
      "cashBalanceFXSegment": 0.0,
      "commodityMarketValue": 0.0,
      "corporateBondsMarketValue": 0.0,
      "key": "LedgerListBASE",
      "dividends": 0.0,
      "exchangeRate": 1.0,
      "funds": 0.0,
      "marketValue": 0.0,
      "optionMarketValue": 0.0,
      "interest": 396687.69214935537,
      "issueOptionsMarketValue": 0.0,
      "moneyFunds": 0.0,
      "netLiquidationValue": 2.0280151634374067E8,
      "realizedPnl": 0.0,
      "unrealizedPnl": 249013.5397937378,
      "secondKey": "BASE",
      "settledCash": 2.0201311791131118E8,
      "severity": 0,
      "stockMarketValue": 391710.74028015137,
      "tBillsMarketValue": 0.0,
      "tBondsMarketValue": 0.0,
      "warrantsMarketValue": 0.0,
      "timestamp": 1700248325
    },
    {
      "key": "LedgerListCAD",
      "timestamp": 1700248325
    },
    {
      "key": "LedgerListUSD",
      "timestamp": 1700248325
    },
    {
      "key": "LedgerListEUR",
      "timestamp": 1700248325
    },
    {
      "key": "LedgerListCHF",
      "timestamp": 1700248325
    }
  ],
  "topic": "sld+DU1234567"
}
Unsubscribe Account Ledger
Copy Location
Unsubscribe from Account Ledger Topic
Topic:
uld
Unsubscribes from account ledger messages for the specified account.
Topic Target:
accountId:
Required
.
Must pass the account ID whose ledger messages will be unsubscribed.
Parameters:
none
uld+DU1234567+{}
Account Ledger Unsubscribe Message
Arrives once.
{
result:
String.
Confirms successful unsubscription.
}
{"result":"unsubscribed from ledger"}
Market Data
Copy Location
Market Data Request
Copy Location
Market Data Request
Topic:
smd
Subscribes the user to watchlist market data.
Streaming, top-of-the-book, level one, market data is available for all instruments using Client Portal API’s websocket endpoint.
IMPORTANT:
Market data streams will terminate after 15 minutes. Users must send a new request for market data after 10 minutes to continue retrieving data for the instrument.
NOTE:
The maximum number of market data subscriptions is based on your account’s
Market Data Lines
.
Topic Target:
conid:
Required.
Must pass a single contract identifier.
Contracts requested use SMART routing by default. To specify the exchange, the contract identifier should be modified to: conId@EXCHANGE, where EXCHANGE is the requested data source.
Combos or Spreads market data can be retrieved using identical formatting to
Combo or Spread Orders
. The only difference is that a spread_conid of 0 must be passed.
Arguments:
fields:
Array of Strings.
Optional.
Pass an array of field IDs. Each ID should be passed as a string.
You can find a list of fields in the Market Data Fields section.
smd+conId+{
    [
    "fields":"field_1",
    "field_2",
    "field_n", 
    "field_n+1"
    ]
}
Watchlist market data at Interactive Brokers is derived from time-based snapshot intervals which vary by product and region. This means that a given tick will only update as frequently as its interval allows. See the table for more details on product specifics.
Product
Frequency
All Products
500ms
Market Data Response
server_id:
String.
Returns the request’s identifier.
conidEx:
String.
Returns the passed conid field. May include exchange if specified in request.
conid:
int.
Returns the contract id of the request
_updated:
int*.
Returns the epoch time of the update in a 13 character integer .
6119:
String.
Field value of the server_id. Returns the request’s identifier.
fields*:
String.
Returns a response for each request. Some fields not be as readily available as others. See the
Market Data
section for more details.
6509:
String.
Returns a multi-character value representing the Market Data Availability.
topic:
String.
Restates the requesting topic.
{
    "server_id":"server_id",
    "conidEx":"conidEx",
    "conid":conid,"
    _updated":_updated,
    "6119":"serverId",
    "field_1":field_1,
    "field_2":field_2,
    "field_n":field_n, 
    "field_n+1":field_n+1,
    "6509":"RB",
    "topic":"smd+conid"
}
Cancel Market Data
Copy Location
Market Data Unsubscribe Request
Topic:
umd
Unubscribes the user from watchlist market data.
Topic Target:
conids:
Required.
Must pass a single contract identifier.
Arguments:
null.
umd+conId+{}
Market Data Unsubscribe Response
No response is returned upon unsubscribing from market data. There will just be an end to the market data from the given conid.
Historical Market Data Request
Copy Location
For streaming historical data, the topic smh+Id is used. There are also optional parameters available in JSON format. If no parameters are specified, the empty parameters array {} can be passed. Incorrectly specified parameters are ignored and the default (empty) response is returned.
NOTE:
Only a max of 5 concurrent historical data request available at a time.
NOTE:
Historical data will only respond once, though customers will still need to unsubscribe from the endpoint.
Historical Data Request
Topic:
smh
Subscribes the user to historical bar data.
Streaming, top-of-the-book, level one, historical data is available for all instruments using Client Portal API’s websocket endpoint.
Topic Target:
conids:
Required.
Must pass a single contract identifier.
Contracts requested use SMART routing by default. To specify the exchange, the contract identifier should be modified to: conId@EXCHANGE, where EXCHANGE is the requested data source.
Arguments:
exchange:
String.
Requested exchange to receive data.
period:
String.
Total duration for which bars will be requested.
bar:
String.
Interval of time to receive data.
outsideRth:
Bool.
Determines if you want data outside regular trading hours (true) or only during market hours (false).
source:
String.
The value determining what type of data to show.
format:
String.
The format in which bars are returned.
smh+conid+{
    "exchange":"exchange",
    "period":"period",
    "bar":"bar",
    "outsideRth":outsideRth,
    "source":"source",
    "format":"format"
}
Historical Data Response
serverId:
String.
Request identifier for the specific historical data request. Used for cancelling the data stream.
symbol:
String.
Returns the symbol for the requested conid.
text:
String.
Company long name.
priceFactor:
int.
Price mutlipler (based on $0.01)
startTime:
String.
Returns the starting time (in epoch time) of the response.
high:
String.
Returns the highest “high value/Volume value/Outside RTH volume” of the period.
low:
String.
Returns the lowest “Low value/Volume value/Outside RTH volume” of the period.
timePeriod:
String.
Returns the period covered by the request.
barLength:
int.
Returns the string length of the bar response.
mdAvailability:
String.
Internal IBKR message.
mktDataDelay:
int.
Returns if there is any delay in the market data.
outsideRth:
Bool.
Returns if the data contains information outside regular trading hours.
volumeFactor:
int.
Determines if the volume is returned as lots, multipliers, or as-is.
priceDisplayRule:
int.
Internal IBKR message.
priceDisplayValue:
String.
Internal IBKR message.
negativeCapable:
Bool.
Returns contract rule whether the contract supports negative values or not.
messageVersion:
int.
Internal IBKR message.
data:
Array of Objects.
Returns all bars related that fall within the period.
o
: float.
Opening value for the bar’s duration.
c
: float.
Closing value for the bar’s duration.
l
: float.
Lowest value for the bar’s duration.
h
: float.
Highest value for the bar’s duration.
v:
int.
Total volume of the bar.
t:
int.
Epoch time of the bar return.
points:
int.
Displays the total number of bars returned within ‘data’.
topic:
String.
Represents the request sent.
{
    "serverId": "serverId",
    "symbol": "symbol",
    "text": "text",
    "priceFactor": priceFactor,
    "startTime": "startTime",
    "high": "high",
    "low": "low",
    "timePeriod": "timePeriod",
    "barLength": barLength,
    "mdAvailability": "mdAvailability",
    "mktDataDelay": mktDataDelay,
    "outsideRth": outsideRth,
    "volumeFactor": volumeFactor,
    "priceDisplayRule": priceDisplayRule,
    "priceDisplayValue": "priceDisplayValue",
    "negativeCapable": negativeCapable,
    "messageVersion": messageVersion,
    "data": [data],
    "points": points, 
    "topic": "topic",
}
The historical market data request takes the following parameters:
Parameter
Description
Valid Values
exchange: String
Contract exchange
Valid exchange on which the contract trades
period: String
Request duration
{1-30}min
{1-8}h
{1-1000}d
{1-792}w
{1-182}m
{1-15}y
bar: String
Request bar size
1min
2min
3min
5min
10min
15min
30min
1h
2h
3h
4h
8h
1d
1w
1m
outsideRTH: Boolean
Request data outside trading hours
true/false
source: String
Type of date requested
midpoint
trades
bid_ask
bid
ask
format: String
Historical values returned
%o – open
%c – close
%h – high
%l – low
%v – volume
Cancel Historical Market Data
Copy Location
Cancel Historical Data Request
Topic:
umh
Unubscribes the user from historical bar data.
Arguments:
serverId: String. Required
serverId is passed initially from the historical data request.
umh+{serverId}
Historical Data Unsubscribe Response
No response is returned upon unsubscribing from historical data. There will just be an end to the historical data stream for the given serverId and one of the five subscriptions will be available again.
Subscribe to BookTrader Price Ladder
Copy Location
Price Ladder Request
Topic:
sbd
Subscribes the user to BookTrader price ladder data.
Streaming BookTrader data requires users to maintain a L2, Depth of Book, market data subscription. See the
Market Data Subscriptions page
for more details.
Topic Target:
acctId:
Required.
Must pass a single AccountId.
conids:
Required.
Must pass a single contract identifier.
exchange:
Optional.
Provide a routing exchange identifier.
If no exchange is specified, all available deep exchanges are assumed.
sbd+acctId+conid+exchange
Price Ladder Response
topic:
String.
Returns the request’s topic string.
data:
Array of Objects.
Returns an array of objects to indicate ladder depth.
row:
int.
Returns the row identifier of the ladder data.
focus:
int.
Indicates if the value was marked as the last trade price for the contract.
price:
String.
Returns the Last, or last executed trade, price.
In some instances, price and size will be returned in the structure ‘”price”:”size @ price”‘.
ask:
String.
Returns the corresponding ask size.
bid:
String.
Returns the corresponding bid size.
{
  "topic":"sbd+acctId+conid",
  "data":[
    {"row":0,"focus":0,"price":"price"},
    {"row":1,"focus":0,"price":"size @ price"},
    {"row":n,"focus":0,"price":"price", "bid":"bid"},
    {"row":n+1,"focus":0,"price":"price", "ask":"ask"},
    {"row":n+1,"focus":0,"price":"size @ price", "ask":"ask"}
  ]
}
Cancel Price Ladder Subscription
Copy Location
Cancel Price Ladder Request
Topic:
ubd
Unsubscribes the user from price ladder data.
Arguments:
acctId:
Required.
Must pass the account ID of the account that made the request.
ubd+{acctId}
Price Ladder Unsubscribe Response
No response is returned upon unsubscribing from the price ladder. There will just be an end to the data stream for the given acctId and the user may subscribe to a new price ladder source.
Miscellaneous Operations
Copy Location
Exercise Options
Copy Location
The operation to exercise via Client Portal is quite involved, and requires that users confirm details across multiple websocket requests.
To initiate the process, developers must make a handshake request passing the “exercise” argument. Then, users will pass in their Option’s ConID to the “CEX” field.
Developers should also maintain
Live Order Updates
while exercising options to confirm final results.
shs+exercise+{"CEX":"Your_Option_Conid"}
This will initially respond with the acknowledgement of the topic.
You will then receive additional messages about the available options to proceed with, including “Cancel” or “Submit”. This will also offer contract information, position information, and an ID to track the request with.
We may also receive a warning notification about this Option exercise, such as in-the-money warnings. This do not need to be suppressed or replied to; however, they should be noted by the trader as they come through.
{"topic":"shs+exercise"}

{"data":{"user_action":[{"id":"submit","text":"Submit"},{"id":"cancel","text":"Cancel"}],"underlying_price":"$211.35","contract":"**AAPL** JUN 28 '24 192.5 Call","underlying_symbol":"AAPL","exercise":{"confirm":false,"confirm_final":false,"enabled":true},"revocable":false,"loading":true,"hold":{"confirm":false,"enabled":true},"qty_lapse":0,"submitted":0,"qty_hold":0,"sec_type":"OPT","qty_exercise":0,"underlying_conid":"265598","morning_expiration":false,"id":5,"position":50,"deadline":"16:25"},"action":"content","MID":"14","topic":"shs+exercise"}

{"data":{"submitted":0,"qty_hold":0,"qty_exercise":0,"warning":"Currently the option is in-the-money by the amount of 18.85 (more than 5 ticks)","exercise":{"confirm":false,"enabled":true},"revocable":false,"loading":false,"qty_lapse":0},"action":"content","MID":"17","topic":"shs+exercise"}
After receiving our second listed message above, we can construct our exercise request. This will use the “inp” topic, along with the exercise argument once again.
Within the brackets, we will pass the “user_input” as our action, and then the data field will contian the order parameters. This will include our ID, which we retrieved from our prior shs+exercise response. We’ll then pass “submit” as our user_action, and then pass our exercise options.
The critical values to observe here are whether you would like your exercise to be final, with “make_final”:true. We also submit our quantity of options to exercise with the “value” parameter. In this case, we are exercising 5 shares.
inp+exercise+{"action":"user_input","data":{"id":"5","user_action":"submit","exercise":{"allowed":"not_shown","make_final":true,"value":5}}}
If there are any additional confirmation/warnings then they will be provided on a new message, including a new “id” value.
{"data":{"user_actions":[{"id":"continue","text":"Continue"},{"id":"cancel","text":"Cancel"}],"id":7,"text":"This exercise request will be final and irreversible. Once submitted, the option position and the stock position will update immediately.","title":"Warning"},"MID":"19","action":"prompt","topic":"inp+exercise"}
As we noticed above, we now would need to request that our exercise continue for id 7 again using the inp+exercise topic.
inp+exercise+{"action":"user_input","data":{"id":"7","user_action":"continue"}
Once the developer submits the new ID with “continue” as their user_action, they will see the order submitted in the SOR websocket.
{"topic":"sor","args":[{"acct":"DU1234567","conidex":"708764406","conid":708764406,"account":"DU1234567","orderId":827785484,"cashCcy":"USD","sizeAndFills":"0/5","orderDesc":"EXERCISE 5, Day","description1":"AAPL","description2":"JUN 28 '24 192.5 Call","ticker":"AAPL","secType":"OPT","remainingQuantity":5.0,"filledQuantity":0.0,"totalSize":5.0,"companyName":"APPLE INC","status":"Inactive","order_ccp_status":"Pending Submit","supportsTaxOpt":"1","lastExecutionTime":"240624150344","bgColor":"#000000","fgColor":"#AFAFAF","isEventTrading":"0","lastExecutionTime_r":1719241424000,"side":"EXER"}]}
Order & Position Operations
Copy Location
Request Live Order Updates
Copy Location
As long as an order is active, it is possible to retrieve it using Client Portal API. Live streaming orders can be requested by subscribing to the sor topic. Once live orders are requested we will start to relay back when there is an update. To receive all orders for the current day the endpoint /iserver/account/orders can be used. It is advised to query all orders for the current day first before subscribing to live orders.
Order Updates Request
Topic:
sor
Subscribes the user to live order updates.
Arguments:
filters
: Array of String
Pass an array with a single string indicating exclusive
Order Status Value
to return.
sor+{"filters":["Submitted"]}
Order Updates Response
topic:
String.
args:
Array of Objects.
acct:
String.
Returns the account Id of which account made the request.
conid:
int.
Contract Identifier for the given order.
orderId:
int.
Order identfier affiliated with the given order.
cashCcy:
String.
Base currency used for the transaction.
sizeAndFills:
String.
Total quantity filled in the order.
orderDesc:
String.
Order description of the given order.
Describes the side, size, orderType, price, and tif of the orer.
description1:
String.
Ticker symbol of the request.
ticker:
String.
Ticker symbol of the request.
secType:
String.
Security type of the request.
listingExchange:
String.
Primary exchange where the contract is held.
remainingQuantity:
float.
Percentage of the order quantity remaining.
filledQuantity:
float.
Percentage of the ordr quantity filled.
companyName:
String.
Longname of the contract’s company.
status:
String.
Current order status.
Value Format: Presubmitted, Submitted, Filled, Cancelled.
origOrderType:
String.
Returns the original order type of the given order.
supportsTaxOpt:
String.
Determines if the order supports Tax Optimizer.
lastExecutionTime:
String.
Returns the datetime object of the most recent execution.
lastExecutionTime_r:
int.
Returns the epoch timestamp of the most recent execution.
order_ref:
string.
Returns the custom order identifier (cOID) from order placement.
orderType:
String.
Returns the current order type of the order.
Value Format: MARKET, LIMIT, STOP
side:
String.
Returns the side of the trade.
Value Format: BUY, SELL
timeInForce:
String.
Returns the time in force for the given order.
price:
int.
Provides the limit or stop price for the submitted order.
bgColor:
String.
Background color. Used for Client Portal only.
fgColor:
String.
Foreground color. Used for Client Portal only.
{
    "topic": "sor" ,
    "args": [
        {
            "acct": "acct",
            "conid": conid,
            "orderId": orderId,
            "cashCcy": "cashCcy",
            "sizeAndFills": "sizeAndFills",
            "orderDesc": "orderDesc",
            "description1": "description1",
            "ticker": "ticker",
            "secType": "secType",
            "listingExchange": "listingExchange",
            "remainingQuantity": remainingQuantity,
            "filledQuantity": filledQuantity,
            "companyName": "companyName",
            "status": "status",
            "origOrderType": "origOrderType",
            "supportsTaxOpt": "supportsTaxOpt",
            "lastExecutionTime": "lastExecutionTime",
            "lastExecutionTime_r": lastExecutionTime_r,
            "order_ref": "order_ref,
            "orderType": "orderType",
            "side": "side",
            "timeInForce": "timeInForce",
            "price": price,
            "bgColor": "#000000",
            "fgColor": "#00F000"
        }
    ]
}
Cancel Live Order Updates
Copy Location
Cancel Order Updates Request
Topic:
uor
Cancels the live order updates subscription.
Arguments:
Do not pass arguments
uor+{}
Cancel Order Updates Response
No response is returned upon unsubscribing from market data. There will just be an end to the market data from the given conid.
Request Profit & Loss
Copy Location
Profit & Loss Request
Topic:
spl
Subscribes the user to live profit and loss information.
Arguments:
Do not pass arguments
spl+{}
Order Updates Response
topic:
String.
Returns the topic of the given request.
args:
Object.
Returns the object containing the pnl data.
acctId.Core:
Object.
Specifies the account for which data was requested.
rowType:
int.
The row value of the request. Will increment with additional accounts.
dpl:
float.
Daily Profit and Loss value.
nl:
float.
Net Liquidity in the account.
upl:
float.
Unrealized Profit and Loss for the day.
uel:
float.
Unrounded Excess Liquidty in the account.
mv:
float
Market value of held stocks in the account.
{ 
   "topic": "spl" , 
    "args": { 
        "acctId.Core": { 
            "rowType":rowType, 
            "dpl":dpl, 
            "nl":nl,
            "upl":upl,
            "uel": uel,
            "mv": mv
        }
    }
}
Cancel Profit & Loss
Copy Location
Cancel Order Updates Request
Topic:
upl
Cancels the subscriptions to profit and loss information.
Arguments:
Do not pass arguments
upl+{}
Cancel Order Updates Response
No response is returned
Request Trades data
Copy Location
Trades Data Request
Topic:
str
Subscribes the user to trades data. This will return all executions data while streamed.
Arguments:
realtimeUpdatesOnly:
bool. Optional
Decide whether you want to display any historical executions, or only the executions available in real time.
Set to false by default.
days:
int. Optional
Returns the number of days of executions for data to be returned.
Set to 1 by default.
str+{
    "realtimeUpdatesOnly":realtimeUpdatesOnly, 
    "days":days
}
Trades Data Response
topic:
String.
Returns the topic of the given request.
args:
Object.
Returns the object containing the pnl data.
execution_id:
String.
Execution identifier of the specific trade.
symbol:
String.
Ticker symbol of the traded contract.
supports_tax_opt:
String.
Determines if the contract supports the tax optimizer. Client Portal only.
side:
String.
Determines if the order was a buy or sell side.
order_description:
String.
Describes the full content of the order.
Value format: “{SIDE} {SIZE} @ {PRICE} on {EXCHANGE}”
trade_time:
String.
Traded date time in UTC.
Value format: “YYYYMMDD-HH:mm:ss”
trade_time_r:
int.
Traded datetime of the execution in epoch time.
size:
float.
Returns the quantity of shares traded.
order_ref:
string.
Returns the custom order identifier (cOID) from order placement.
price:
String.
Returns the price used for the given trade.
exchange:
String.
Returns the exchange the order executed at.
net_amount:
float.
Returns the total amount traded after calculating multiplier.
account:
String.
Returns the account the order was traded on.
accountCode:
String.
Returns the account the order was traded on.
company_name:
String.
Returns the title of the company for the contract.
contract_description_1:
String.
Returns the underlying symbol of the contract.
contract_description_2:
String.
Returns a full description of the derivative.
sec_type:
String.
Returns the security type traded.
conid:
int.
Contract identifier for the traded contract.
conidEx:
String.
Returns the conidEx of the order if specified. Otherwise returns conid.
open_close:
String.
Returns if the execution was a closing trade.
Returns “???” if the position was already open, but not a closing order.
liquidation_trade:
String.
Returns if the trade was a result of liquidation.
is_event_trading:
String.
Determines if the order can be used with EventTrader.
{
  "topic":"topic"
  "args":[
    {
    "execution_id":"execution_id"
    "symbol":"symbol"
    "supports_tax_opt":"supports_tax_opt"
    "side":"side"
    "order_description":"order_description"
    "trade_time":"trade_time"
    "trade_time_r":trade_time_r
    "size":size
    "order_ref": "order_ref"
    "price":"price"
    "exchange":"exchange"
    "net_amount":net_amount
    "account":"account"
    "accountCode":"accountCode"
    "company_name":"company_name"
    "contract_description_1":"contract_description_1"
    "contract_description_2":"contract_description_2"
    "sec_type":"sec_type"
    "conid":conid
    "conidEx":"conidEx"
    "open_close":"open_close"
    "liquidation_trade":"liquidation_trade"
    "is_event_trading":"is_event_trading"
    }
  ]
}
Cancel Trades data
Copy Location
Cancel Trades Data Request
Topic:
utr
Cancels the trades data subscription
utr
Cancel Trades Data Response
Nothing is returned upon cancellation request.
Session
Copy Location
Websocket topic message required for session connectivity.
Maintain Session (Ping)
Copy Location
Websocket Ping request
Topic:
tic
Ping the websocket in order to keep the websocket session alive.
To maintain a session for accessing /iserver or /ccp endpoints, use the topic
tic
. It is advised to ping the session at least once per minute.
Note:
It is still required to send a request to the /tickle endpoint every few minutes or when the session expires (/sso/validate is returning a 0).
Do not pass arguments
tic
Unsolicited Messages
Copy Location
In many instances, Interactive Brokers will automatically return various messages over the websocket to alert the user to various issues. None of these messages can be directly requested but will be returned as certain events arise.
Account Updates
Copy Location
Returns details about the brokerage accounts that the currently logged in user has access to. An initial message is sent when the user a connection to the websocket is first established, with supplemental messages are sent whenever there is a change to the account details.
topic:
String.
Returns the topic of the given request.
args:
Object.
Returns the object containing the pnl data.
accounts:
Array.
Displays all accounts currently accessible by the user.
acctProps:
Object.
Returns an object detailing the account properties.
acctId:
Object.
Returns the specific allocation group or account information.
hasChildAccounts:
bool.
Returns whether there are any subaccounts attached to the listed account.
supportsCashQty:
bool.
Returns where the account supports cash quantity orders.
noFXConv:
bool.
Returns if the account supports forex conversion.
isProp:
bool.
supportsFractions:
bool.
Returns if the account supports fractional trading.
allowCustomerTime:
bool.
Returns if the account returns data in the customer’s local time.
aliases:
Object.
Returns a series of accounts and their affiliated aliases.
allowFeatures:
Object.
Displays the allowed features for the account.
showGFIS:
bool.
Determines whether the account can display data or not.
showEUCostReport:
bool.
Determines if the account receives the EU Cost Report.
allowEventContract:
bool.
Determines if the account can receive event contracts.
allowFXConv:
bool.
Determines if the account allows forex conversions.
allowFinancialLens:
bool.
Determines if the account supports Financial Lens (Client Portal Only).
allowMTA:
bool.
Determines if the account supports Mobile Trading Alerts.
allowTypeAhead:
bool.
Determines if the account supports Type Ahead (Client Portal Only).
allowEventTrading:
bool.
Determines if the account supports Event Trader (Client Portal Only).
snapshotRefreshTimeout:
int.
Determines if the account can support snapshot refresh (Client Portal Only).
liteUser:
bool.
Returns if the account is an IBKR Lite account.
showWebNews:
bool.
Returns if the account
research:
bool.
Determines if the account supports research subscriptions.
debugPnl:
bool.
Determines if the account enables debug for PnL (Client Portal Only).
showTaxOpt:
bool.
Determines if the account supports Tax Optimizer (Client Portal Only).
showImpactDashboard:
bool.
Determines if the account should display the Impact Dashboard on startup (Client Portal Only).
allowDynAccount:
bool.
Determines if the account supports Dynamic Account Structures (Client Portal Only).
allowCrypto:
bool.
Determines if the account supports Crypto trading.
allowedAssetTypes:
String.
Returns all support asset or security types.
chartPeriods:
Object of Arrays.
Returns the trading hours supported by each approvided asset type.
groups:
Array.
Lists all groups the account is listed under.
profiles:
Array.
Lists all profiles the account is listed under.
selectedAccount:
String.
Returns the acively selected account.
serverInfo:
Object.
Returns a description of the server info.
sessionId:
String.
Returns the sesion identifier.
isFT:
bool.
Returns if the fractional trading account.
isPaper:
bool.
Returns if the active account is a paper trading account.
{
    "topic":"act",
    "args":{
       "accounts":[],
       "acctProps":{
          "All":{
             "hasChildAccounts":hasChildAccounts,
             "supportsCashQty":supportsCashQty,
             "noFXConv":noFXConv,
             "isProp":isProp,
             "supportsFractions":supportsFractions,
             "allowCustomerTime":allowCustomerTime
          }
       },
       "aliases":{},
       "allowFeatures":{
          "showGFIS":showGFIS,
          "showEUCostReport":showEUCostReport,
          "allowEventContract":allowEventContract,
          "allowFXConv":allowFXConv,
          "allowFinancialLens":allowFinancialLens,
          "allowMTA":allowMTA,
          "allowTypeAhead":allowTypeAhead,
          "allowEventTrading":allowEventTrading,
          "snapshotRefreshTimeout":snapshotRefreshTimeout,
          "liteUser":liteUser,
          "showWebNews":showWebNews,
          "research":research,
          "debugPnl":debugPnl,
          "showTaxOpt":showTaxOpt,
          "showImpactDashboard":showImpactDashboard,
          "allowDynAccount":allowDynAccount,
          "allowCrypto":allowCrypto,
          "allowedAssetTypes":"allowedAssetTypes"
       },
       "chartPeriods":{
          "STK":[],
          "CFD":[],
          "OPT":[],
          "FOP":[],
          "WAR":[],
          "IOPT":[],
          "FUT":[],
          "CASH":[],
          "IND":[],
          "BOND":[],
          "FUND":[],
          "CMDTY":[],
          "PHYSS":[],
          "CRYPTO":[]
       },
       "groups":[],
       "profiles":[],
       "selectedAccount":"selectedAccount",
       "serverInfo":{
          "serverName":"serverName",
          "serverVersion":"serverVersion"
       },
       "sessionId":"sessionId",
       "isFT":isFT,
       "isPaper":isPaper
    }
 }
Authentication Status
Copy Location
When initially connecting to the websocket endpoint, the topic sts will relay back the current authentication status of the user. Authentication status updates, for example those resulting from competing sessions, are also relayed back to the websocket client via this topic.
topic:
String.
Returns the topic of the given request.
args:
Object.
Returns the data object.
authenticated
:
bool.
Returns whether the user is authenticated to the brokerage session.
{
    "topic": "sts" ,
    "args": {
        "authenticated": authenticated
    }
}
Bulletins
Copy Location
If there are urgent messages concerning exchange issues, system problems, and other trading information, the topic blt is sent along with the message argument and a unique identifier for the bulletin.
topic:
String.
Returns the topic of the given request.
args:
Object.
Returns the bulletins argument values.
id:
String.
Returns the ID for the specific bulletin.
message:
String.
Returns the bulletin information.
{
    "topic": "blt" ,
    "args": {
        "id": "id" ,
        "message": "message" 
    }
}
System Connection Messages
Copy Location
When initially connecting to websocket the topic system relays back a confirmation with the corresponding username. While the websocket is connecting every 10 seconds there after a heartbeat with corresponding unix time (in millisecond format) is relayed back.
topic:
String.
Returns the topic of the given request.
success:
String.
Returns the username logged in with that has built the websocket.
{
    "topic": "system" ,
    "success": "success"
}
Notifications
Copy Location
If there is a brief message regarding trading activity the topic ntf will be sent.
topic:
String.
Returns the topic of the given request.
args:
Object.
Returns the object containing the pnl data.
id:
String.
Returns the identifier for the specific notification.
text:
String.
Returns the body text for the affiliated notification.
title:
String.
Returns the title or headline for the notification.
url:
String.
If relevant, provides a url where a user can go to read more about the notification.
{
    "topic": "ntf",
    "args": {
        "id": "id",
        "text": "text",
        "title": "title",
        "url": "url"
    }
}
OAuth 1.0a
Copy Location
Introduction
Copy Location
Interactive Brokers offers an OAuth 1.0a authentication procedure for licensed Financial Advisors, Organizations, IBrokers, and third party services. Beyond the initial authentication procedure, the OAuth implementation will behave the same as the standard
Client Portal Gateway
.
Authentication via tokens produced by our OAuth 1.0a workflow permits requests to be made directly to
https://api.ibkr.com
, without the need for any intermediary software such as the Client Portal Gateway. Resource paths remain the same regardless of the method of authentication.
Interactive Brokers makes a distinction between first-party use of OAuth directly by clients and third-party use of OAuth by vendors of software, described below.
First Party Oauth
Copy Location
Interactive Brokers classifies first party entities as institutions that will be trading on behalf of themselves or their institution. The same entity developing with the API platform will be the same entity that will be using it for trading.
Examples of first party entities include financial advisors, hedge funds, and organizations looking to trade their own capital.
For interested first party candidates, please email apiintegration@interactivebrokers.com with the following questions answered.
What do you intend to do with OAuth access?
Please list all accounts that will use the developed OAuth program.
Will the client application be developed in-house or by a third-party developer?
Approved groups using First Party OAuth will need to use the Self Service Portal in order to generate their consumer key, encryption keys, and access tokens. This link will be provided directly to approved entities during the onboarding process. This is an essential step in creating your program and following the steps listed below.
Registering a Consumer Key via the Self-Service Portal will not be valid until after Midnight in New York, United States; Zug, Switzerland, or Hong Kong depending on your region. Attempting to use the new Consumer Key prior to the reset will result in an error (you may receive a 401 Invalid Consumer error).
First Party OAuth Workflow
Copy Location
Once you are registered in the Self-Service portal, all authentication will begin with the /live_session_token endpoint. Then you may begin making
Authenticated Requests With OAuth 1.0A
for our
Standard Endpoints
.
Live Session Token Endpoint
Important:
First Party OAuth registrants can not use the /request_token, /authorize, or /access_token endpoints. Attempts to do so will result in error.
Third Party OAuth
Copy Location
Interactive Brokers classifies third party entities as any organization offering a platform or medium of trading to individuals outside of the organization. Please note that interested groups that would like to register as a third party with Interactive Brokers must have an established platform with other brokerage firms, or a full proof of concept with an integration using the Client Portal API.
Examples of this would include auto-traders or robo-advisors, public mobile applications, and groups forwarding market data.
For organizations that meet the criteria above and have an interest in integrating with Interactive Brokers, we would ask that you please email
api-solutions@interactivebrokers.com
.
To give you a sense for the process ahead:
Our onboarding team conducts the initial vetting process. Once we have collected a sufficient amount of information, we will complete a preliminary review. Estimated time to complete this step is 2-3 weeks. Assuming we are able to proceed, we will send your application to our Compliance team for review.
IBKR Compliance conducts an enhanced due diligence review on all third party applicants, followed by a three tier approval process. Estimated time to complete Compliance related reviews and tasks is 3-6 weeks.
If Compliance approval is reached, our Legal team will generate the WebAPI agreement which we will send to you for review and signature. In parallel, we will ask you to provide public keys and a CallbackURL which will be required in to generate your consumer key and finalize the setup. Detailed instructions for this process will be provided once we reach this stage. Estimated time for our side to complete the aforementioned work and processes is 3-5 weeks.
The above timelines are all estimations and can vary. We recommend providing as much information as possible up front. Not doing so can exaggerate timelines.
During the enhanced due diligence reviews conducted by our Compliance teams, they will expect to see that 3rd Party Vendors looking to offer their services to our clients have a completed website with details on all of the features/services that will be provided and finalized details on their offering. This typically includes a clear user work flow for all components and details on their functionality, capability.
As mentioned previously, should Compliance approval be reached
we would be able to generate a live consumer key for you and a
ny significant changes to the offering after it has been approved (like addition of trading functionality) would require additional review and approval from our Compliance Teams before being offered to IBKR clients.
Please be aware we expect 3
rd
Parties offering automated trading solutions would hold applicable registration with financial authority in all regions they plan offer the service, unless you are able to provide support (i.e. a legal opinion) as to why the business provided would not require registration that location. Additionally, t
he offering will need to be reviewed and approved by Compliance teams in all regions you intend to support IBKR clients.
The above timelines are all estimations and can vary. We recommend providing as much information as possible up front. Not doing so can exaggerate timelines.
Third Party OAuth Workflow
Copy Location
Once approved for Third Party OAuth, developers must use the supplied certificates and consumer key to register, authorize, and generate an access token for a user before they are able to retrieve a live session token for
Authenticated Requests With OAuth 1.0A
.
1. Generate a Request Token
2. Authorize The Consumer Key
3, Generate Access Tokens
4. Generate a Live Session Token
Understanding TESTCONS
Copy Location
When onboarding new clients, Support may often recommend getting started with the TESTCONS consumer key.
TESTCONS is a Third Party Consumer Key, and in order to register it, developers must initially follow the third party workflow.
For First Party developers, please utilize one of the provided samples to register the consumer key with your user. After registration, you may reserve the Access Token and Access Token Secret to follow the standard First party OAuth implementation.
This consumer key is only available for Paper Trading Accounts. Attempts to register this consumer key with a live account will result in an error.
Third Party consumer keys will register to an account immediately. As a result, TESTCONS will be immediately usable for any registered client, regardless of whether the individual is applying for a first party or third party consumer key.
OAuth 1.0a Python Libraries
Copy Location
The OAuth implementation content shown below is built using Python 3.12. Clients looking to directly implement this content through Python will need the Python libraries listed to proceed.
pycryptodome // This must be installed with pip as it is not part of the Python Standard Library.
requests
base64
json
time
datetime
Request Token
Copy Location
For Third Party OAuth users to start the OAuth process, we must first get a request token.
To get a request token, an OAuth request to
https://api.ibkr.com/v1/api/oauth/request_token
must be made.
The request should be a POST request but with no body. Remember that
an authorization header
has to be authorize the connection.
This step is a good indicator of whether or not something is wrong with your OAuth request. If you are missing any portion of the authorization header, the response will tell you so. If something is wrong with either the base string or signature creation, then you will be met with a 401 response.
Important:
If you are a First Party OAuth users, do not follow this step. You will receive an error. For developers implementing First Party OAuth, proceed directly to
Requesting the Live Session Token
.
Endpoint
Copy Location
Note
we do not return an oauth_token_secret in the response as we are using RSA signatures rather than PLAINTEXT authentication.
POST /oauth/request_token
OAuth Parameters
Copy Location
oauth_consumer_key:
String. Required
The 25-character hexadecimal string that was obtained from Interactive Brokers during the OAuth consumer registration process.
oauth_signature_method:
String. Required
The signature method used to sign the request. Currently only ‘RSA-SHA256’ is supported.
oauth_signature:
String. Required
The signature for the request generated using the method specified in the oauth_signature_method parameter. See section 9 of the OAuth v1.0a specification for more details on signing requests.
oauth_timestamp:
String. Required
Timestamp expressed in seconds since 1/1/1970 00:00:00 GMT. Must be a positive integer and greater than or equal to any timestamp used in previous requests.
oauth_nonce:
String. Required
A random string uniquely generated for each request.
oauth_callback:
String. Required
An absolute URL to which IB will redirect the user. This URL would be provided to the onboarding team during initial integration, or ‘oob’ can be provided for localhost development.
{
  "oauth_consumer_key": "TESTCONS",
  "oauth_signature_method": "RSA-SHA256",
  "oauth_signature": "fnHGXncCkcnB3U3jYxjh%2BgUdo7PelK5NQGIedbDeAQgDtO02ccLVapH5QtpazS%2BKlwg7bJTAgxsM1T5QOox6IjBQJu91EJ%2FFVUCFtd8rNbRQNGbEWdibglWErBDuHY%2FVCLRHdCqAg9BhV%2BZ7FTY6oCT9HSQr6ZK%2FgNqTd58vpt5z8cPCoPHRJN4HzB54J5A4K7R7aEx9s1B5wqIer7fdVIzKb0KSSpP44Hx%2BGnLfZINfQHIrRCfFwbeGEvi31PF9ICWRIKZz5LxqX6OtT0Dze8LiZo7PnkrA5b0m9PR0cP1v6DSBVaSyJXBITiml9Gyn2HuiexHjRJt85gPhNiU3VA%3D%3D",
  "oauth_timestamp": "1722886851",
  "oauth_nonce": "a568f4e3b0e161b0dcf187331b8274be",
  "oauth_callback": "oob"
}
Request
Copy Location
url = f'https://api.ibkr.com/v1/api/oauth/request_token'
oauth_params = {
  "oauth_callback": {{oauth_callback }},
  "oauth_consumer_key": {{consumer_key}},
  "oauth_nonce": hex(random.getrandbits(128))[2:],
  "oauth_signature_method": "RSA-SHA256",
  "oauth_timestamp": str(int(datetime.now().timestamp())),
  }
params_string = "&".join([f"{k}={v}" for k, v in sorted(oauth_params.items())])
# Base string successfully created
base_string = f"POST&{quote_plus(url)}&{quote(params_string)}"

# Base string should then signed with the private key in RSA-SHA256
encoded_base_string = base_string.encode("utf-8")
sha256_hash = SHA256.new(data=encoded_base_string)
bytes_pkcs115_signature = PKCS1_v1_5_Signature.new(
rsa_key=signature_key
).sign(msg_hash=sha256_hash)
b64_str_pkcs115_signature = base64.b64encode(bytes_pkcs115_signature).decode("utf-8")

# Establish the authorization header
oauth_params["oauth_signature"] = quote_plus(b64_str_pkcs115_signature)
oauth_params["realm"] = realm
oauth_header = "OAuth " + ", ".join([f'{k}="{v}"' for k, v in sorted(oauth_params.items())])
headers = {"authorization": oauth_header}
headers["User-Agent"] = "python/3.11"
  
request_request = requests.post(url=url, headers=headers)
if request_request.status_code == 200:
    rToken = request_request.json()["oauth_token"]
Response
Copy Location
oauth_token:
String.
Resulting request token used as an encoded authentication value.
Be sure to save the request token for your next two requests.
After receiving your access token, this value will no longer be used.
{
  "oauth_token": "b9082d68cfef06b030de"
}
Authorization
Copy Location
After retrieving our request token, we need to authorize the value against the Interactive Brokers server. This is done by directing users to https://interactivebrokers.com/authorize?oauth_token={{ REQUEST_TOKEN }} where they will log in with their Interactive Brokers credentials. Because we are using “oob” as our callback url, users will be presented with an error page.
Replace REQUEST_TOKEN with the request token you generated
After the user logs in, they will be redirected to a URL specified during consumer key creation, and there will be two query parameters in the URL:
oauth_token and oauth_verifier
oauth_token is the request token, and oauth_verifier is the verifier token required for the next step.
An example of url after the user logs in
https://localhost:20000/?oauth_token=b9082d68cfef06b030de&oauth_verifier=0ffb93ab9aa0d2177cc2
Important:
If you are a First Party OAuth users, do not follow this step. You will receive an error. For developers implementing First Party OAuth, proceed directly to
Requesting the Live Session Token
.
Endpoint
Copy Location
Direct the client to the Interactive Brokers authorize uri where they can then log in to establish the request token to their user.
https://interactivebrokers.com/authorize?oauth_token={{ Request Token }}
Request
Copy Location
For our programmatic implementation without an official callback url, we can introduce a simple line to direct the user to the login page and save the verifier token for later.
url = f'https://interactivebrokers.com/authorize?oauth_token={rToken}'
vToken = input(f"Please log in to {url} and paste the 'oauth_verifier' value here: ")
Response
Copy Location
A successful login will direct them to the callback url, which displays both the oauth_token (request token) and the oauth_verifier (verifier token). This can be pasted back to our original request, and saved as a variable for the Access Token request.
Access Token
Copy Location
A POST request to https://api.ibkr.com/v1/api/oauth/access_token must now be made.
This time, oauth_verifier must be added to the authorization header, with the value being the verifier token retrieved from the previous step.
oauth_token must also be added to the authorization header, the value being
the request token
.
If the request succeeds, the response will contain two values: oauth_token and oauth_token_secret.
The oauth_token in the response is the access token, and the oauth_token_secret will be used for the next step.
Important:
If you are a First Party OAuth users, do not follow this step. You will receive an error The access token and access token secret would otherwise be retrieved through the Self Service Portal. For developers implementing First Party OAuth, proceed directly to
Requesting the Live Session Token
.
Endpoint
Copy Location
The Access Token endpoint will be used to return the Access Token and Access Token Secret values to be used for all requests moving forward as an identifier of the user with our consumer key.
An Access Token will remain the same whenever a username is generated with a given consumer key; however, the access token secret will be unique upon each generation.
POST /oauth/access_token
OAuth Params
Copy Location
oauth_consumer_key:
String. Required
The 25-character hexadecimal string that was obtained from Interactive Brokers during the OAuth consumer registration process.
oauth_token:
String. Required
The request token obtained from IB via /request_token.
oauth_signature_method:
String. Required
The signature method used to sign the request. Currently only ‘RSA-SHA256’ is supported.
oauth_signature:
String. Required
The signature for the request.
oauth_timestamp:
String. Required
Timestamp expressed in seconds since 1/1/1970 00:00:00 GMT. Must be a positive integer and greater than or equal to any timestamp used in previous requests.
oauth_nonce:
String. Required
A random string uniquely generated for each request.
oauth_verifier:
String. Required
The verification code received from IB after the user has granted authorization.
{
  "oauth_consumer_key": "TESTCONS",
  "oauth_token": "47b63b7961c51e1df1e6",
  "oauth_signature_method": "RSA-SHA256",
  "oauth_signature": "V9p9e41Zx8Fsi1QkJq3QewZdt%2BZM8GTCcKswY08MbZKCHsob57JEdNbpeANWkiwqVdDnRArQ52ifQsutYlvXvsUQAVd2vuiMqcEqpGN8c2ZmTqVQbQwNaqw0LLXQ84DmwDUWJa%2F8pSJPmCmMPi4tJPKb%2Bta1DyVN2ec8KwzRw8c7MpKsbKBXXC%2B0vJ7Y8kTE0WnoiPA%2FJ8sRn7sZnlsDlEmyxNY%2Fggrr%2F5GJTQFe2EXka5eMsHrnWTYdC1tg38%2BVebt4HNyptLUCO0%2FpeZVRbjN3RyfPlDpSlJ6jGh2lqtcyLEueDsxvN%2FsRWjpiBl1in%2Bou6YYeB2D%2FBz%2Bttvpagw%3D%3D",
  "oauth_timestamp": "1722030398",
  "oauth_nonce": "734860ccbc2f14ae971a8cf1a6ec936",
  "oauth_verifier": "01028463e73960e27b3f"
}
Request
Copy Location
url = f'https://api.ibkr.com/v1/api/oauth/access_token'
oauth_params = {
  "oauth_callback":callback,
  "oauth_consumer_key": consumer_key,
  "oauth_nonce": hex(random.getrandbits(128))[2:],
  "oauth_signature_method": "RSA-SHA256",
  "oauth_timestamp": str(int(datetime.now().timestamp())),
  "oauth_token": rToken,
  "oauth_verifier": vToken,
  }
params_string = "&".join([f"{k}={v}" for k, v in sorted(oauth_params.items())])

# Base string successfully created
base_string = f"POST&{quote_plus(url)}&{quote(params_string)}"

# Base string should then signed with the private key in RSA-SHA256
encoded_base_string = base_string.encode("utf-8")
sha256_hash = SHA256.new(data=encoded_base_string)
bytes_pkcs115_signature = PKCS1_v1_5_Signature.new(
  rsa_key=signature_key
  ).sign(msg_hash=sha256_hash)
b64_str_pkcs115_signature = base64.b64encode(bytes_pkcs115_signature).decode("utf-8")

# Establish the authorization header
oauth_params["oauth_signature"] = quote_plus(b64_str_pkcs115_signature)
oauth_params["realm"] = realm
oauth_header = "OAuth " + ", ".join([f'{k}="{v}"' for k, v in sorted(oauth_params.items())])
headers = {"authorization": oauth_header}
headers["User-Agent"] = "python/3.11"

# Send the request and save the tokens to variables
atoken_request = requests.post(url=url, headers=headers)
aToken = atoken_request.json()["oauth_token"]
aToken_secret = atoken_request.json()["oauth_token_secret"]
Response
Copy Location
oauth_token:
String.
Resulting oauth or access token used as an encoded authentication value.
oauth_token_secret:
String.
Resulting access token secret used as an encoded authentication value.
{
  "oauth_token": "e84c11dc149cb96ee5bb",
  "oauth_token_secret": "BGJsaMbLdQA6WZd+8AYxBaBFuOlLIZAQJwKzLseTbwK8KsTyghX1LVI5Gjh0T/m3j3lQNGbxDoyxageGdNsQVQIS+QrkYVeePfptBzB6fPqwdnT66miP4J80Aoo3Xv5gJeeHnqMK3YNSEzK09idE8Id66YeeNiAYzfrNdrZ5CC+V3oS7giaqankY2Fz7rxN95rBHGqKEzkMf9109f25yLEauPvA+7rD4iyIwpfZJVI8q1/D/tBprIklTJ/QuAcbbiDY4AoYH744A4IDS5CHMYK1/XIUlSMpFmamip5GOAjiORNEKuR2r93kyeZwUuFyosudHeuZexgvE72enfS9gqg=="
}
Live Session Token
Copy Location
The final step in the OAuth authorization process is the live session token. This is the final stage of authorizing your user for each session. In this step we must calculate a Diffie-Hellman challenge using the prime and generator in the Diffie-Hellman spec provided when registering your consumer key.
If you are an IB customer who registered using the Self-Service OAuth page then on that same page you should have completed
the Access Token step
. You would now proceed to this final step to complete the OAuth authorization process.
Endpoint
Copy Location
The live session token will allow the user to access their API, for trading or for portfolio access, over a 24 hour period. The creation of the Live Session Token does not establish a complete trading session, as that would be handled by
Initializing the Brokerage Session
.
POST /oauth/live_session_token
Diffie-Hellman Random Value
Copy Location
The Diffie-Hellman random value is simply any positive random 256-bit integer value.
This will be used immediately for the Diffie-Hellman challenge as well as the computation of the live session token.
Python
C#
dh_random = str(random.getrandbits(256))
Random random = new();

BigInteger dh_random = random.Next(1, int.MaxValue);
Diffie-Hellman Challenge
Copy Location
The Diffie-Hellman challenge value is the quotient of modulus division, converted to a hex string. Using ‘2’ as our generator, raised to the power of our Diffie-Hellman random value, divided by our Diffie-Hellman Prime or Modulus value.
Python
C#
# Replace with path to DH param PEM file.
with open("./dhparam.pem, "r") as f:
    dh_param = RSA.importKey(f.read())
    dh_prime = dh_param.n # Also known as DH Modulus
    dh_generator = dh_param.e  # always =2

# Convert result to hex and remove leading 0x chars.
dh_challenge = hex(pow(base=dh_generator, exp=dh_random, mod=dh_prime))[2:]
// Extract our dh_modulus and dh_generator values from our dhparam.pem file's bytes.
AsnReader asn1Seq = new AsnReader(dh_der_data, AsnEncodingRules.DER).ReadSequence();
BigInteger dh_modulus = asn1Seq.ReadInteger();
BigInteger dh_generator = asn1Seq.ReadInteger();

// Generate our dh_challenge value by calculating the result of our generator to the power of our random value, modular divided by our dh_modulus.
BigInteger dh_challenge = BigInteger.ModPow(dh_generator, dh_random, dh_modulus);
Prepend
Copy Location
We can find the prepend by first converting our access token secret to a bytestring. We then decrypt the bytestring using our private encryption key as an RSA key with PKCS1v1.5 padding. The prepend is the resulting bytestring converted to a hex string value.
Python
C#
# Replace with path to private encryption key file.
with open("./private_encrpytion.pem", "r") as f:
    encryption_key = RSA.importKey(f.read())

bytes_decrypted_secret = PKCS1_v1_5_Cipher.new(
    key=encryption_key
    ).decrypt(
        ciphertext=base64.b64decode(access_token_secret), 
        sentinel=None,
        )
prepend = bytes_decrypted_secret.hex()
// Create the crypto provider 
RSACryptoServiceProvider bytes_decrypted_secret = new()
{
  // Utililze a keysize of 2048 rather than the default 7168
  KeySize = 2048
};

StreamReader sr = new("./private_encryption.pem");
string reader = sr.ReadToEnd();
sr.Close();

// Find the pem field content from the StreamReader string
PemFields pem_fields = PemEncoding.Find(reader);

// Convert the pem base 64 string content into a byte array for use in our import
byte[] der_data = Convert.FromBase64String(reader[pem_fields.Base64Data]);

// Import the bytes object as our key
bytes_decrypted_secret.ImportPkcs8PrivateKey(der_data, out _);

// Encode the access token secret as an ASCII bytes object
byte[] encryptedSecret = Convert.FromBase64String(access_token_secret);

// Decrypt our secret bytes with the encryption key
byte[] raw_prepend = bytes_decrypted_secret.Decrypt(encryptedSecret, RSAEncryptionPadding.Pkcs1);

// Convert our bytestring to a hexadecimal string
string prepend = Convert.ToHexString(raw_prepend).ToLower();
OAuth Params
Copy Location
oauth_consumer_key:
String. Required
The 9-character string that was obtained from Interactive Brokers during the OAuth consumer registration process. This is set in the Self Service Portal.
oauth_token:
String. Required
The access token obtained from IB via /access_token or the Self Service Portal.
oauth_signature_method:
String. Required
The signature method used to sign the request. Currently only ‘RSA-SHA256’ is supported.
oauth_signature:
String. Required
The signature for the request generated using the method specified in the oauth_signature_method parameter. See section 9 of the OAuth v1.0a specification for more details on signing requests.
oauth_timestamp:
String. Required
Timestamp expressed in seconds since 1/1/1970 00:00:00 GMT. Must be a positive integer and greater than or equal to any timestamp used in previous requests.
oauth_nonce:
String. Required
A random string uniquely generated for each request.
diffie_hellman_challenge:
String. Required
Challenge value calculated using the Diffie-Hellman prime and generated provided during the registration process. See the “OAuth at Interactive Brokers” document for more details.
{
  "diffie_hellman_challenge": "5356ee6f78fc204b22e2012636d23116e4158ee84aa4451c4a8d3f595ec83434497073e25697ab23cc912799dadeef39fe243d317f193659e488535a31dbcb814600ffad3fd76b076e7f1c54cf045395c1f01d982a358f3202dd6b546271498040f4687959b7b240bc6222902d24e1bf5de42ae0a46cc60f41f62a58d428932a92e5954e7980384a9e1e0b918a35f0a838e0c4c3d0cb32db759b5cbda371e035740d9c0030b1619b61e928b8d12ca141bd3fe74ac10a835382125a57837c84b5bd1873bd118f92657b8dd45e48652093e5c0c3a5dacfb4d140e5672ddc05eb1d90bc29c433e744ae8950e96590668a9b8503e596780b14852be639ce3b5ba2c0",
  "oauth_consumer_key": "TESTCONS",
  "oauth_token": "e84c11dc149cb96ee5bb",
  "oauth_signature_method": "RSA-SHA256",
  "oauth_signature": "czbA1dRKJSBdwn5GYxAJQCmCAqfZ6dyOa%2FgmuY%2F5Lhub64cSeQUzKp8vGrF6afnXhCiIXnHsCTONK7uNbRu2V%2FE%2FziQ57BWfbAEzH98kQdWAlWqqmaxXBzbg%2Fr1AZDRP%2FYWrEggNvJaHjbkWaotcrAWpsfxVLcdc3Sl7kXmbFYN0u20MjLUD7q5yDrJT5TXw9JC2xvFimJj65WxqyICZizQUUrg35KRQKaxytQFdwqf5RS6B65gmoi7gHXZcDu2zDWGhe67bZKV8myd0isIJZBs8a5alGd33n7Y1V7pv5Ux9hFOEHEBzSaE3kn9dqw%2Fp5w%2Fl%2F0xiOQGpXWvPRVA2uA%3D%3D",
  "oauth_timestamp": "1722886871",
  "oauth_nonce": "e8181dd345bcc1a7237df79cd1b59219",
  "realm": "test_realm"
}
Encoded Base String
Copy Location
The Encoded Base String for the Live Session Token is composed of the Prepend, Method, “&”, URL, “&”, and OAuth params combined as a sorted string.
Both the URL and the parameter string
must
be URI escaped according to
Rfc3986
.
Python
C#
params_string = "&".join([f"{k}={v}" for k, v in sorted(oauth_params.items())])
method = 'POST'
url = f'https://{baseUrl}/oauth/live_session_token'
base_string = f"{prepend}{method}&{quote_plus(url)}&{quote(params_string)}"
encoded_base_string = base_string.encode("utf-8")
// Sort our oauth_params dictionary by key.
Dictionary<string, string> sorted_params = oauth_params.OrderBy(pair => pair.Key).ToDictionary(pair => pair.Key, pair => pair.Value);

// Combine our oauth_params into a single string for our base_string.
string params_string = string.Join("&", sorted_params.Select(kv => $"{kv.Key}={kv.Value}"));

// Create a base string by combining the prepend, url, and params string.
string base_string = $"{prepend.ToLower()}POST&{EscapeUriDataStringRfc3986(lst_url)}&{EscapeUriDataStringRfc3986(params_string)}";

// Convert our new string to a bytestring 
byte[] encoded_base_string = Encoding.UTF8.GetBytes(base_string);
OAuth Signature
Copy Location
Creating the OAuth Signature is a multi-stage process.
First uses will need to create a sha256 hash of the encoded base string
Next, you will need to create a PKCS1 v1.5 (
Rfc2313
) bytestring signature using your Private Encryption Key as an RSA key to sign our sha256 hash value created in our prior step.
Once your bytestring has been generated, we will need to Base 64 encode our bytes, and then decode them using UTF-8 to receive a string value.
Finally, we will need to URI escape our string, again using
Rfc3986
, to receive our final oauth_signature value.
Python
C#
# Generate SHA256 hash of base string bytestring.
sha256_hash = SHA256.new(data=encoded_base_string)

# Generate bytestring PKCS1v1.5 signature of base string hash.
# RSA signing key is private signature key.
bytes_pkcs115_signature = PKCS1_v1_5_Signature.new(
    rsa_key=signature_key
    ).sign(msg_hash=sha256_hash)

# Generate str from base64-encoded bytestring signature.
b64_str_pkcs115_signature = base64.b64encode(bytes_pkcs115_signature).decode("utf-8")

# URL-encode the base64 signature str and add to oauth params dict.
oauth_params['oauth_signature'] = quote_plus(b64_str_pkcs115_signature)
// Create a Sha256 Instance
SHA256 sha256_inst = SHA256.Create();

// Generate SHA256 hash of base string bytestring.
byte[] sha256_hash = sha256_inst.ComputeHash(encoded_base_string);

// Create the crypto provider for our signature
RSACryptoServiceProvider bytes_pkcs115_signature = new()
{
    // Utililze a keysize of 2048 rather than the default 7168
    KeySize = 2048
};

// Use our function to retrieve the object bytes
StreamReader sr = new(signature_fp);
string reader = sr.ReadToEnd();
sr.Close();

// Find the pem field content from the StreamReader string
PemFields pem_fields = PemEncoding.Find(reader);

// Convert the pem base 64 string content into a byte array for use in our import
byte[] sig_der_data = Convert.FromBase64String(reader[pem_fields.Base64Data]);

// Import the bytes object as our key
bytes_pkcs115_signature.ImportPkcs8PrivateKey(sig_der_data, out _);

//Generate the Pkcs115 signature key
RSAPKCS1SignatureFormatter rsaFormatter = new(bytes_pkcs115_signature);

rsaFormatter.SetHashAlgorithm("SHA256");

//Receive the bytestring of our signature
byte[] signedHash = rsaFormatter.CreateSignature(sha256_hash);

// Convert the bytestring signature to base64.
string b64_str_pkcs115_signature = Convert.ToBase64String(signedHash);

// URL-encode the base64 signature str and add to oauth params dict.
oauth_params.Add("oauth_signature", EscapeUriDataStringRfc3986(b64_str_pkcs115_signature));
Realm
Copy Location
The realm is a required oauth parameter. The realm will only ever be one of two values.
If you are using the “TESTCONS” consumer key during your paper testing, you will need to use “test_realm”
Python
C#
# Oauth realm param omitted from signature, added to header afterward.
oauth_params["realm"] = "test_realm"
// Oauth realm param omitted from signature, added to header afterward.
oauth_params.Add("realm", :"test_realm");
Once you are using your own consumer key, you must use “limited_poa”.
Python
C#
# Oauth realm param omitted from signature, added to header afterward.
oauth_params["realm"] = "limited_poa"
// Oauth realm param omitted from signature, added to header afterward.
oauth_params.Add("realm", :"limited_poa");
Authorization Header
Copy Location
The Authorization Header compiles our full OAuth parameters into an alphabetically-sorted string.
Each key/value pair must be added in the format ‘key=\”value\”, where each value is surrounded with quotes
Each pair separated with a comma.
The string must be prepended with “OAuth “.
The final string should be added as a header for your request using the “Authorization” header.
Python
C#
# Assemble oauth params into auth header value as comma-separated str.
oauth_header = "OAuth " + ", ".join([f'{k}="{v}"' for k, v in sorted(oauth_params.items())])

# Create dict for LST request headers including OAuth Authorization header.
headers = {"Authorization": oauth_header}
Dictionary fin_sorted_params = oauth_params.OrderBy(pair => pair.Key).ToDictionary(pair => pair.Key, pair => pair.Value);

// Assemble oauth params into auth header value as comma-separated str.
string oauth_header = $"OAuth " + string.Join(", ", fin_sorted_params.Select(kv => $"{kv.Key}=\"{kv.Value}\""));

// Add our Authorization header to our request's header container.
request.Headers.Add("Authorization", oauth_header);
Required Headers
Copy Location
In order to make a successful request, several headers must be included. Some libraries and modules may automatically include these values, though they must always be received by Interactive Brokers in order to successfully process request.
Accept: This must be set to “*/*”
Accept-Encoding: This must be set to “gzip,deflate”
Authorization: See our prior
Authorization Header
step.
Connection: This must be set to “keep-alive”.
Host: This must be set to “api.ibkr.com”
User-Agent: This may be anything, though the browser identifier or request language is suggested.
Python
C#
# Add User-Agent header, required for all requests. Can have any value.
headers = {
    "Authorization":oauth_header,
    "User-Agent":"python/3.11"
}
// Build out our request headers
request.Headers.Add("Authorization", oauth_header);
request.Headers.Add("Accept", "*/*");
request.Headers.Add("Accept-Encoding", "gzip,deflate");
request.Headers.Add("Connection", "keep-alive");
request.Headers.Add("Host", "api.ibkr.com");
request.Headers.Add("User-Agent", "csharp/6.0");
Request
Copy Location
# Generate a random 256-bit integer.
dh_random = random.getrandbits(256)

# Compute the Diffie-Hellman challenge:
# generator ^ dh_random % dh_prime
# Note that IB always uses generator = 2.
# Convert result to hex and remove leading 0x chars.
dh_challenge = hex(pow(base=dh_generator, exp=dh_random, mod=dh_prime))[2:]
# --------------------------------
# Generate LST request signature.
# --------------------------------

# Generate the base string prepend for the OAuth signature:
#   Decrypt the access token secret bytestring using private encryption
#   key as RSA key and PKCS1v1.5 padding.
#   Prepend is the resulting bytestring converted to hex str.
bytes_decrypted_secret = PKCS1_v1_5_Cipher.new(
    key=encryption_key
    ).decrypt(
        ciphertext=base64.b64decode(access_token_secret), 
        sentinel=None,
        )
prepend = bytes_decrypted_secret.hex()

# Put prepend at beginning of base string str.
base_string = prepend
# Elements of the LST request so far.
method = 'POST'
url = f'https://{baseUrl}/oauth/live_session_token'
oauth_params = {
    "oauth_consumer_key": consumer_key,
    "oauth_nonce": hex(random.getrandbits(128))[2:],
    "oauth_timestamp": str(int(datetime.now().timestamp())),
    "oauth_token": access_token,
    "oauth_signature_method": "RSA-SHA256",
    "diffie_hellman_challenge": dh_challenge,
}

# Combined param key=value pairs must be sorted alphabetically by key
# and ampersand-separated.
params_string = "&".join([f"{k}={v}" for k, v in sorted(oauth_params.items())])

# Base string = method + url + sorted params string, all URL-encoded.
base_string += f"{method}&{quote_plus(url)}&{quote(params_string)}"

# Convert base string str to bytestring.
encoded_base_string = base_string.encode("utf-8")
# Generate SHA256 hash of base string bytestring.
sha256_hash = SHA256.new(data=encoded_base_string)

# Generate bytestring PKCS1v1.5 signature of base string hash.
# RSA signing key is private signature key.
bytes_pkcs115_signature = PKCS1_v1_5_Signature.new(
    rsa_key=signature_key
    ).sign(msg_hash=sha256_hash)

# Generate str from base64-encoded bytestring signature.
b64_str_pkcs115_signature = base64.b64encode(bytes_pkcs115_signature).decode("utf-8")

# URL-encode the base64 signature str and add to oauth params dict.
oauth_params['oauth_signature'] = quote_plus(b64_str_pkcs115_signature)

# Oauth realm param omitted from signature, added to header afterward.
oauth_params["realm"] = realm

# Assemble oauth params into auth header value as comma-separated str.
oauth_header = "OAuth " + ", ".join([f'{k}="{v}"' for k, v in sorted(oauth_params.items())])

# Create dict for LST request headers including OAuth Authorization header.
headers = {"Authorization": oauth_header}

# Add User-Agent header, required for all requests. Can have any value.
headers["User-Agent"] = "python/3.11"

# Prepare and send request to /live_session_token, print request and response.
lst_request = requests.post(url=url, headers=headers)

# Check if request returned 200, proceed to compute LST if true, exit if false.
if not lst_request.ok:
    print(f"ERROR: Request to /live_session_token failed. Exiting...")
    raise SystemExit(0)

# Script not exited, proceed to compute LST.
response_data = lst_request.json()
dh_response = response_data["diffie_hellman_response"]
lst_signature = response_data["live_session_token_signature"]
lst_expiration = response_data["live_session_token_expiration"]
Response
Copy Location
diffie_hellman_response:
String.
Response based on the calculated Diffie Hellman challenge.
The full value should be 512 characters long.
live_session_token_signature:
String.
Signature value used to prove authenticated status for subsequent requests.
live_session_token_expiration:
Number.
Returns the epoch timestamp of the live session token’s expiration.
The live session token is valid for approximately 24 hours after creation.
{
  "diffie_hellman_response": "62933e{...}d64d6db34d",
"live_session_token_signature": "9bd5922b2b79effef23c6fb03cc715dcdc8d6219",
"live_session_token_expiration": 1700691802316
}
Computing The Live Session Token
Copy Location
From the
Live Session Token Response Object
, we only received the Diffie-Hellman response, live session token signature, and live session token expiration values. We now need to use the signature and response values in order to compute our live session token.
Prepend Bytes
Copy Location
We first need to convert our Prepend hex string value into bytes. This will be used to generate a HMAC Hash later.
Python
C#
# Generate bytestring from prepend hex str.
prepend_bytes = bytes.fromhex(prepend)
//Generate bytestring from prepend hex str.
byte[] prepend_bytes = Convert.FromHexString(prepend);
Calculating K
Copy Location
To calculate our live session token, we need to use modulus division to receive a K value for a HMAC hash.
Begin by pulling the base value, B, which will be our dh_response value from our /live_session_token response, using a leading 0 as a sign bit for the hex string.
We will then need to retrieve a BigInteger value of the dh_response value.
Our exponent value would be equivalent to the same dh_random value used for our /live_session_token request.
Our modulus, p, would be the same as our dh_modulus or dh_prime value from our Diffie-Hellman file.
We would finally calculate K using the formula B^a modulo p.
Python
C#
# K will be used to hash the prepend bytestring (the decrypted access token) to produce the LST.
B = int(dh_response, 16)
a = dh_random
p = dh_prime
K = pow(B, a, p)
// Validate that our dh_response value has a leading sign bit, and if it's not there then be sure to add it.
if (dh_response[0] != 0)
{
    dh_response = "0" + dh_response;
}

// Convert our dh_response hex string to a biginteger. 
BigInteger B = BigInteger.Parse(dh_response, NumberStyles.HexNumber);

BigInteger a = dh_random;
BigInteger p = dh_modulus;

// K will be used to hash the prepend bytestring (the decrypted access token) to produce the LST.
BigInteger K = BigInteger.ModPow(B, a, p);
Once K is receive, convert the integer to its hex string representation before converting it to a byte array. In some cases, the resultant K value will have an odd number of leading characters that should be prepended by an additional 0.
Python
C#
# Generate hex string representation of integer K.
hex_str_K = hex(K)[2:]

# If hex string K has odd number of chars, add a leading 0, because all Python hex bytes must contain two hex digits  (0x01 not 0x1).
if len(hex_str_K) % 2:
    print("adding leading 0 for even number of chars")
    hex_str_K = "0" + hex_str_K

# Generate hex bytestring from hex string K.
hex_bytes_K = bytes.fromhex(hex_str_K)

# Prepend a null byte to hex bytestring K if lacking sign bit.
if len(bin(K)[2:]) % 8 == 0:
    hex_bytes_K = bytes(1) + hex_bytes_K
// Generate hex string representation of integer K. Be sure to strip the leading sign bit.
string hex_str_k = K.ToString("X").ToLower(); // It must be converted to lowercase values prior to byte conversion.

// If hex string K has odd number of chars, add a leading 0
if (hex_str_k.Length % 2 != 0)
{
    // Set the lead byte to 0 for a positive sign bit.
    hex_str_k = "0" + hex_str_k;
}

// Generate hex bytestring from hex string K.
byte[] hex_bytes_K = Convert.FromHexString(hex_str_k);
Final Live Session Calculation
Copy Location
To calculate the Live Session Token, we need to create a new HMAC Sha1 object, using the K hex bytes as a key. We then hash our HMAC Sha1 object against our prepend byte string.
The final byte array, converted to a Base64 string, is the computed live session token.
Python
C#
bytes_hmac_hash_K = HMAC.new(
    key=hex_bytes_K,
    msg=prepend_bytes,
    digestmod=SHA1,
    ).digest()
# The computed LST is the base64-encoded HMAC hash of the hex prepend bytestring. Converted here to str.
computed_lst = base64.b64encode(bytes_hmac_hash_K).decode("utf-8")
// Create HMAC SHA1 object
HMACSHA1 bytes_hmac_hash_K = new()
{
    // Set the HMAC key to our passed intended_key byte array
    Key = hex_bytes_K
};
// Hash the SHA1 bytes of our key against the msg content.
byte[] K_hash = bytes_hmac_hash_K.ComputeHash(prepend_bytes);

// Convert hash to base64 to retrieve the computed live session token.
string computed_lst = Convert.ToBase64String(K_hash);
Validate Live Session Token
Copy Location
It should be noted that this step may be skipped for your final product, though it is essential during initial development stages to validate implementation.
After calculating our Live Session Token, the calculation may be validated through the following steps to re-calculate the lst_signature value retrieved from the /live_session_token endpoint. If the calculated value matches the return value, then we have a valid live session token. If the two do not match, there is an issue in your LST generation process.
Begin by converting the computed live session token value to a base64 decoded byte array.
Next, retrieve the UTF-8 byte array equivalent of our consumer key.
Create a new HMAC Sha1 object, using the decoded live session token as a key. We then hash our HMAC Sha1 object against our consumer key byte string.
Convert the resultant byte array to a hex string.
If our new hex string matches the received lst_signature value, then our computed_lst value may be used as the live_session_token in future requests. If the two are different, then there may be an issue in the live_session_token generation process.
Python
C#
# Generate hex-encoded str HMAC hash of consumer key bytestring.
# Hash key is base64-decoded LST bytestring, method is SHA1.
hex_str_hmac_hash_lst = HMAC.new(
    key=base64.b64decode(computed_lst),
    msg=consumer_key.encode("utf-8"),
    digestmod=SHA1,
).hexdigest()

# If our hex hash of our computed LST matches the LST signature received in response, we are successful.
if hex_str_hmac_hash_lst == lst_signature:
    live_session_token = computed_lst
    print("Live session token computation and validation successful.")
    print(f"LST: {live_session_token}; expires: {datetime.fromtimestamp(lst_expiration/1000)}\n")
else:
    print(f"ERROR: LST validation failed.")
//Generate hex - encoded str HMAC hash of consumer key bytestring.
// Hash key is base64 - decoded LST bytestring, method is SHA1
byte[] b64_decode_lst = Convert.FromBase64String(computed_lst);

// Convert our consumer key str to bytes
byte[] consumer_bytes = Encoding.UTF8.GetBytes(consumer_key);

// Hash the SHA1 bytes against our hex bytes of K.
byte[] hashed_consumer = EasySha1(b64_decode_lst, consumer_bytes);

// Convert hash to base64 to retrieve the computed live session token.
string hex_lst_hash = Convert.ToHexString(hashed_consumer).ToLower();

// If our hex hash of our computed LST matches the LST signature received in response, we are successful.
if (hex_lst_hash == lst_signature)
{
    string live_session_token = computed_lst;
    Console.WriteLine("Live session token computation and validation successful.");
    Console.WriteLine($"LST: {live_session_token}; expires: {lst_expiration}\n");
}
else
{
    Console.WriteLine("######## LST MISMATCH! ########");
    Console.WriteLine($"Hexed LST: {hex_lst_hash} | LST Signature: {lst_signature}\n");
}
Authenticated Requests With OAuth 1.0A
Copy Location
After successfully computing a live session token, the client would now be able to make requests. The next logical step is to make a request to /iserver/auth/ssodh/init endpoint to initialize a brokerage session as an example of standard requests.
Initial OAuth Params
Copy Location
The OAuth params of standard requests are slightly different than the live session token.
oauth_consumer_key:
String. Required
A 9-character string set during your OAuth registration process.
Third Party OAuth: This will be provided by Interactive Brokers during the registration process.
First Party OAuth: This is saved in the Self Service Portal.
oauth_nonce:
String. Required
A random string uniquely generated for each request.
oauth_timestamp:
String. Required
Timestamp expressed in seconds since 1/1/1970 00:00:00 GMT. Must be a positive integer and greater than or equal to any timestamp used in previous requests.
oauth_token:
String. Required
Third Party OAuth: This is received from the
Access Token endpoint
First Party OAuth: This is generated in the Self Service Portal.
oauth_signature_method:
String. Required
The signature method used to sign the request. Only ‘HMAC-SHA256’ is supported.
Python
C#
oauth_params = {
    "oauth_consumer_key": consumer_key,
    "oauth_nonce": hex(random.getrandbits(128))[2:],
    "oauth_signature_method": "HMAC-SHA256",
    "oauth_timestamp": str(int(datetime.now().timestamp())),
    "oauth_token": access_token
}
// Interactive Brokers requires a 10 digit Unix timestamp value.
// Values beyond 10 digits will result in an error.
string timestamp = DateTimeOffset.Now.ToUnixTimeMilliseconds().ToString();
timestamp = timestamp.Substring(0, timestamp.Length - 3);

// Create a Random object, and then retrieve any random positive integer value.
Random random = new();

String oauth_nonce = random.Next(1, int.MaxValue).ToString("X").ToLower();

//Create a dictionary for all oauth params in our header.
Dictionary<string, string> oauth_params = new()
{
    { "oauth_consumer_key", consumer_key },
    { "oauth_nonce", oauth_nonce },
    { "oauth_timestamp", timestamp },
    { "oauth_token", access_token },
    { "oauth_signature_method", "HMAC-SHA256" }
};
Encoded Base String
Copy Location
The Encoded Base String for the standard requests is composed of the Method, “&”, URL, “&”, and OAuth params combined as a sorted string.
Both the URL and the parameter string
must
be URI escaped according to
Rfc3986
.
Python
C#
params_string = "&".join([f"{k}={v}" for k, v in sorted(oauth_params.items())])
method = 'POST'
url = f'https://{baseUrl}/oauth/live_session_token'
base_string = f"{method}&{quote_plus(url)}&{quote(params_string)}"
encoded_base_string = base_string.encode("utf-8")
// Sort our oauth_params dictionary by key.
Dictionary<string, string> sorted_params = oauth_params.OrderBy(pair => pair.Key).ToDictionary(pair => pair.Key, pair => pair.Value);

// Combine our oauth_params into a single string for our base_string.
string params_string = string.Join("&", sorted_params.Select(kv => $"{kv.Key}={kv.Value}"));

// Create a base string by combining the prepend, url, and params string.
string base_string = $"POST&{EscapeUriDataStringRfc3986(lst_url)}&{EscapeUriDataStringRfc3986(params_string)}";

// Convert our new string to a bytestring 
byte[] encoded_base_string = Encoding.UTF8.GetBytes(base_string);
OAuth Signature
Copy Location
Creating the OAuth Signature is a multi-stage process.
First uses will need to create an HMAC Sha256 hash of the encoded base string
You must use a Base64 byte array of our live session token as the Key value.
Then, hash the encoded byte string using our new HMAC Sha256 object.
Convert the resulting bytes into a Base64 encoded string.
Then URI escape our string, again using
Rfc3986
, to receive our final oauth_signature value.
Python
C#
# Generate bytestring HMAC hash of base string bytestring.
# Hash key is base64-decoded LST bytestring, method is SHA256.
bytes_hmac_hash = HMAC.new(
    key=base64.b64decode(live_session_token), 
    msg=base_string.encode("utf-8"),
    digestmod=SHA256
    ).digest()

# Generate str from base64-encoded bytestring hash.
b64_str_hmac_hash = base64.b64encode(bytes_hmac_hash).decode("utf-8")

# URL-encode the base64 hash str and add to oauth params dict.
oauth_params["oauth_signature"] = quote_plus(b64_str_hmac_hash)
// Create HMAC SHA256 object
HMACSHA256 bytes_hmac_hash_K = new()
{
    // Set the HMAC key to our live_session_token
    Key = Convert.FromBase64String(computed_lst)
};

// Hash the SHA256 bytes against our encoded bytes.
byte[] K_hash = bytes_hmac_hash_K.ComputeHash(encoded_base_string);

// Generate str from base64-encoded bytestring hash.
string b64_str_hmac_hash = Convert.ToBase64String(K_hash);

// URL-encode the base64 hash str and add to oauth params dict.
oauth_params.Add("oauth_signature", EscapeUriDataStringRfc3986(b64_str_hmac_hash));
Realm
Copy Location
The realm is a required oauth parameter. The realm will only ever be one of two values.
If you are using the “TESTCONS” consumer key during your paper testing, you will need to use “test_realm”
Python
C#
# Oauth realm param omitted from signature, added to header afterward.
oauth_params["realm"] = "test_realm"
// Oauth realm param omitted from signature, added to header afterward.
oauth_params.Add("realm", :"test_realm");
Once you are using your own consumer key, you must use “limited_poa”.
Python
C#
# Oauth realm param omitted from signature, added to header afterward.
oauth_params["realm"] = "limited_poa"
// Oauth realm param omitted from signature, added to header afterward.
oauth_params.Add("realm", :"limited_poa");
Authorization Header
Copy Location
The Authorization Header compiles our full OAuth parameters into an alphabetically-sorted string.
Each key/value pair must be added in the format ‘key=\”value\”, where each value is surrounded with quotes
Each pair separated with a comma.
The string must be prepended with “OAuth “.
The final string should be added as a header for your request using the “Authorization” header.
Python
C#
# Assemble oauth params into auth header value as comma-separated str.
oauth_header = "OAuth " + ", ".join([f'{k}="{v}"' for k, v in sorted(oauth_params.items())])

# Create dict for LST request headers including OAuth Authorization header.
headers = {"Authorization": oauth_header}
Dictionary fin_sorted_params = oauth_params.OrderBy(pair => pair.Key).ToDictionary(pair => pair.Key, pair => pair.Value);

// Assemble oauth params into auth header value as comma-separated str.
string oauth_header = $"OAuth " + string.Join(", ", fin_sorted_params.Select(kv => $"{kv.Key}=\"{kv.Value}\""));

// Add our Authorization header to our request's header container.
request.Headers.Add("Authorization", oauth_header);
Required Headers
Copy Location
In order to make a successful request, several headers must be included. Some libraries and modules may automatically include these values, though they must always be received by Interactive Brokers in order to successfully process request.
Accept: This must be set to “*/*”
Accept-Encoding: This must be set to “gzip,deflate”
Authorization: See our prior
Authorization Header
step.
Connection: This must be set to “keep-alive”.
Host: This must be set to “api.ibkr.com”
User-Agent: This may be anything, though the browser identifier or request language is suggested.
Python
C#
# Add User-Agent header, required for all requests. Can have any value.
headers = {
    "Authorization":oauth_header,
    "User-Agent":"python/3.11"
}
// Build out our request headers
request.Headers.Add("Authorization", oauth_header);
request.Headers.Add("Accept", "*/*");
request.Headers.Add("Accept-Encoding", "gzip,deflate");
request.Headers.Add("Connection", "keep-alive");
request.Headers.Add("Host", "api.ibkr.com");
request.Headers.Add("User-Agent", "csharp/6.0");
Submit The Request
Copy Location
After calculating our Authorization header creating our headers, we can submit the request. As mentioned in our
Endpoints section
, all content would then be submitted as JSON formatting. Examples in the respective languages have been included for our example.
Python
C#
json_data = {"publish":True, "compete":True}

# end request to /ssodh/init, print request and response.
init_request = requests.post(url=url, headers=headers, json=json_data)
if init_request.status_code == 200:
    print(init_request.content)
string req_content = JsonSerializer.Serialize(new { compete = true, publish = true });
StringContent req_content_json = new(req_content, Encoding.UTF8, "application/json");
request.Content = req_content_json;
HttpResponseMessage response = client.SendAsync(request).Result;
Flex Web Service
Copy Location
The Flex Web Service is a small, standalone HTTP API for programmatically generating and retrieving pre-configured Flex Queries. Flex Queries are first constructed manually as templates in Client Portal, after which the Flex Web Service API is used to generate an instance of a report populated with up-to-date data and deliver it back to the requesting client. The Flex Web Service offers access to the same Flex Query reports that you’d otherwise retrieve manually from within Client Portal.
For more information about Flex Queries and about IB’s reporting functionality overall, please consult the following documentation:
Flex Queries
Reporting Guide
Usage Notes:
Though Flex Query reports can be generated and retrieve at any time, the data they contain will not necessarily change throughout the day. “Activity Statement” Flex Queries contain data that is only updated once daily at close of business, so there is no benefit to generating and retrieving these reports more than once per day. Normally one would retrieve the prior day’s Activity Statements at the start of the following day, which guarantees that all values have been updated by IB’s reporting backend.
“Trade Confirmation” Flex Queries will yield updated data throughout the day as executions occur against working orders, but these execution entries are also not available in Trade Confirmation Flex Queries in real-time. Typically a new execution will be available for inclusion in a newly generated Flex Query report within 5 to 10 minutes.
Given the above restrictions on the refresh rate of Flex Query data, the Flex Web Service is not suitable for active polling for newly generated reports. Rather, it is best used to capture the desired reports once daily, or at most intermittently throughout the day in the case of Trade Confirmation reports.
Depending on the size of the report to be generated, there may be a slight delay between the initial request to generate the report and the report’s availability via the second request. Time to availability is also dependent on system utilization, so please permit some flexibility in the timing of the final report retrieval, either via an explicit “wait” between the first and second requests, or via periodic reattempts of the second request.
Note that the same Flex Query reports (as well as many other report types) can also be scheduled for delivery via email or FTP:
Scheduled Delivery of User-Defined Flex Queries
Scheduled Delivery of IB-Defined Statements
Flex queries using a variable duration, such as “Last N Days” will always use the maximum possible days for a given request, rather than following the last used number of days in Client Portal. It is recommended to use the precise request values such as “Last Month”, “Last Quarter”, “Year To Date”, etc.
Client Portal Configuration
Copy Location
Before using the Flex Web Service API to programmatically retrieve Flex Query reports, you’ll need to manually obtain some values from within
Client Portal
:
An access token to authenticate our requests
One or more query IDs corresponding to the reports you’d like to fetch
If you want to retrieve Flex Queries that you’ve already created, you’ll need to log into Client Portal with the username that created them, as these Flex Query report configurations are username-specific. Once we obtain these values, however, the use of the Flex Web Service API does not involve your IB credentials. Your username will only be involved in any subsequent management of the access token or reconfiguration of the report templates.
Note:
these steps can only be completed by logging in to the Client Portal directly.
Enable and Create Access Token
Copy Location
Navigate to the “Reporting” Tab, and select “Flex Queries”. Here, you can find information about your Activity Flex Query, Trade Confirmation Flex Query, Flex Query Delivery, and Flex Web Service Configuration.
We will need to start by selecting the “Flex Web Service Configuration” section on the right.
IMPORTANT:
Linked accounts may only view the Flex Web Service Token from the Master account of the structure. The “Flex Web Service Configuration” may not be visible when viewing subaccounts for advisors.
The Flex Web Service Token may be used to retrieve reporting data for all linked accounts, dependent on account inclusion during flex query generation.
A flex query will only be visible when searching for the same selection of accounts.
Advisors, for example, that create Flex Queries when an individual account is selected will not be able to view that same query when the Advisor account and individual account are selected.
On the new page, click the empty box next to
Flex Web Service Status
to enable the Flex Web Service, and then click “Save” to save your credentials. This will enable the flex web service and provide a new “Current Token” value that will be used in your flex web service requests moving forward.
You also have the option to generate a new token. A token can be specified to remain active anywhere between 6 hours and 1 year. Generating a new token also allows users to restrict which IPs can make flex web service requests.
Create a Flex Query
Copy Location
Returning to the Flex Queries page, we will now need to generate at least one Activity Flex Query or Trade Confirmation Flex Query.
Next, click on the
Info
icon to the left of a Flex Query.
From the info pop-over, capture the
Query ID
number listed at the top. This will be used when generating your report.
Including Audit Trail Fields
Copy Location
The “Audit Trail Field” will opt to retrieve data from the Audit Trail rather than a front-end resource. This will directly affect data that can be returned in certain Sections and the Options therein. As an example,
OrderReference
will only be displayed if Audit Trail Fields are set to “Yes”.
This extends to Brokerage Order ID, Order Reference, Volatility Order Link, and Order Placement Time.
Note:
These features are currently unavailable for Paper Trading Accounts, and would only be available in the Live trading environment.
Using Flex Web Service
Copy Location
The Flex Web Service API consists of two endpoints. Obtaining Flex Query reports via the Flex Web Service API is a two-step workflow involving requests to both endpoints in sequence. First, you will make a request to trigger IB’s backend to generate an instance of your report, which will populate your Flex Query template with the available data. In response, you’ll receive a reference code identifying this particular instance of the report. Next, you’ll make a request to fetch the generated report using the reference code.
Note that all requests must include a User-Agent header.
Generate a Report
Copy Location
To begin, you’ll make a GET request to the
/SendRequest
endpoint, passing your access token and the desired Flex Query template’s query ID as query parameters:
https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest?t={CurrentToken}&q={QueryID}&v=3
Make request to /SendRequest
Copy Location
IMPORTANT:
The /SendRequest endpoint maintains a pacing limitation of 1 request per second. A maximum of 10 requests per minute may be submitted.
Query Params
t
.
Required
Accepts the
Current Token
created for the Flex Web Service in Client Portal’s Account Settings interface.
q
.
Required
The
Query ID
identifier for the desired report template, obtained from the Client Portal’s Flex Query interface.
v
.
Required, leave value = 3
Specifies the
version
of the Flex Web Service to be used. Values
2
and
3
are supported, but version 3 should always be used.
Python
requestBase = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
token = 528191644107458877539776
queryId = 800969
flex_version = 3

send_path = "/SendRequest"
send_params = {
    "t":token, 
    "q":queryId, 
    "v":flex_version
}

flexReq = requests.get(url=requestBase+send_slug, params=send_params)
Success response from /SendRequest
Copy Location
Status
. A value of
Success
indicates a successful request to generate the report. If the request failed, Status will deliver
Fail
.
ReferenceCode
. If the request was successful, the XML response will contain this numeric reference code. This code will be used in the subsequent request to retrieve the generated Flex Query.
url
. This is a legacy URL. Should be ignored.
<FlexStatementResponse timestamp="28 August, 2012 10:37 AM EDT">
    <Status>Success</Status>
    <ReferenceCode>1234567890</ReferenceCode>
   <url>https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.GetStatement</url>
</FlexStatementResponse>
Failure response from /SendRequest
Copy Location
Status
. A failed request will deliver a Status of
Fail
.
ErrorCode
.
A numeric code indicating the nature of the failure. See
Error Codes
for a list of error code values and their descriptions.
ErrorMessage
.
A human-readable description of the error. See
Error Codes
for a list of error code values and their descriptions.
<FlexStatementResponse timestamp="28 August, 2012 10:37 AM EDT">
    <Status>Fail</Status>
    <ErrorCode>1012</ErrorCode>
    <ErrorMessage>Token has expired.</ErrorMessage>
</FlexStatementResponse>
Retrieve the Report
Copy Location
Next, you’ll make a GET request to the
/GetStatement
endpoint, again passing your access token, but now passing the reference code obtained from the prior endpoint:
https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement?t={AccessToken}&q={ReferenceCode}&v=3
Depending on the size of the request, you may need to wait longer between the /SendRequest and /GetStatement endpoint calls for the full report to finish generating.
Make request to /GetStatement
Copy Location
Query Params
t
.
Required
Accepts the
Access Token
created for the Flex Web Service in Client Portal’s Account Settings interface.
q
Required
Accepts the
ReferenceCode
returned by the previous successful request, which identifies the instance of the report to be retrieved. Note that a given Flex Query template can be used to generate multiple reports over time, each populated with data at the time of generation, so this ReferenceCode identifier is used to retrieve a specific instance, presumably the one generated immediately prior.
v
.
Required, leave value = 3
Specifies the
version
of the Flex Web Service to be used. Values
2
and
3
are supported, but version 3 should always be used.
Python
tree = ET.ElementTree(ET.fromstring(flexReq.text))
root = tree.getroot()

for child in root:
    if child.tag == "Status":
        if child.text != "Success":
            print(f"Failed to generate Flex statement. Stopping...")
            exit()
    elif child.tag == "ReferenceCode":
        refCode = child.text

print("Hold for Request.")
time.sleep(20)

receive_slug = "/GetStatement"
receive_params = {
    "t":token, 
    "q":refCode, 
    "v":flex_version
}

receiveUrl = requests.get(url=requestBase+receive_slug, params=receive_params, allow_redirects=True)

open(csvPath, 'wb').write(receiveUrl.content)
Error Codes
Copy Location
The following is a consolidated list of error codes returnable by the
/SendRequest
and
/GetStatement
endpoints when a server-side failure occurs.
ErrorCode
ErrorMessage
1001
Statement could not be generated at this time. Please try again shortly.
1003
Statement is not available.
1004
Statement is incomplete at this time. Please try again shortly.
1005
Settlement data is not ready at this time. Please try again shortly.
1006
FIFO P/L data is not ready at this time. Please try again shortly.
1007
MTM P/L data is not ready at this time. Please try again shortly.
1008
MTM and FIFO P/L data is not ready at this time. Please try again shortly.
1009
The server is under heavy load. Statement could not be generated at this time. Please try again shortly.
1010
Legacy Flex Queries are no longer supported. Please convert over to Activity Flex.
1011
Service account is inactive.
1012
Token has expired.
1013
IP restriction.
1014
Query is invalid.
1015
Token is invalid.
1016
Account in invalid.
1017
Reference code is invalid.
1018
Too many requests have been made from this token. Please try again shortly.
Limited to one request per second, 10 requests per minute (per token).
1019
Statement generation in progress. Please try again shortly.
1020
Invalid request or unable to validate request.
1021
Statement could not be retrieved at this time. Please try again shortly.
IBKR Campus Newsletters
IBKR Campus
Bi-Weekly Newsletter
Get updates on podcasts, webinars, courses, and more from our IBKR pillars.
Sign Up
Traders' Insight
Daily Newsletter
View the latest financial news articles from the top voices in the industry.
Sign Up
IBKR Webinars
Weekly Newsletter
Get updates on podcasts, webinars, courses, and more from our IBKR pillars.
Sign Up
IBKR Quant
Weekly Newsletter
For those wanting to trade markets using computer-power by coders and developers.
Sign Up
IBKR API
Monthly Newsletter
Hear about the latest tools and techniques from our own IBKR API staff.
Sign Up
This website uses cookies to collect usage information in order to offer a better browsing experience. By browsing this site or by clicking on the "ACCEPT COOKIES" button you accept our Cookie Policy.
Accept Cookies
Reject All Cookies
Manage Cookies
Traders' Insight RSS
×
To add IBKR Traders’ Insight to your RSS Feed, please paste the following link into your reader:
Copy RSS
IBKR Quant RSS
×
To add IBKR Quant to your RSS Feed,
please paste the following link into your reader:
Copy RSS
Attention
This page contains information regarding Options Trading
To view this page, you must acknowledge that you have received the Characteristics & Risks of Standardized Options, also known as the options disclosure document (ODD). Options involve risk and are not suitable for all investors. For more information, click here to read the "
Characteristics and Risks of Standardized Options
" or visit:
ibkr.com/occ
By acknowledging this disclosure you are also allowing this website to use "functional" cookies on your browser. To find out more about cookies, see our privacy settings.
Privacy Settings
I DO NOT Acknowledge
I have read the Options Disclosure Document
Privacy Preference Center
Your Privacy
Strictly Necessary
Performance Cookies
Functional Cookies
Marketing Cookies
Cookie Policy
Your Privacy
When you visit any website it may use cookies and web beacons to store or retrieve information on your browser. This information might be about you, your preferences or your device and is typically used to make the website work as expected. The information does not usually directly identify you, but can provide a personalized browsing experience. Because we respect your right to privacy, you can choose not to allow some types of cookies and web beacons. Please click on the different category headings to find out more and change our default settings. However, blocking some types of cookies may impact your experience on our website and limit the services we can offer.
Strictly Necessary Cookies
Strictly necessary cookies are necessary for the website to function and cannot be switched off in our systems. They are typically set in response to actions made by you which amount to a request for services, such as setting your privacy preferences, logging in or filling in forms. While you can set your browser to block or alert you about these cookies, some parts of the website will not work. These cookies do not store any personally identifiable information.
ALWAYS ACTIVE
Performance Cookies
Performance cookies and web beacons allow us to count visits and traffic sources so we can measure and improve website performance. They help us to know which pages are the most and least popular and see how visitors navigate around our website. All information these cookies and web beacons collect is aggregated and anonymous. If you do not allow these cookies and web beacons we will not know when you have visited our website and will not be able to monitor its performance.
Allow Performance Cookies:
Yes
No
Functional Cookies
Functional cookies enable our website to provide enhanced functionality and personalization. They may be set by us or by third party providers whose services we have added to our pages. If you do not allow these cookies then some or all of these services may not function properly.
Allow Functional Cookies:
Yes
No
Marketing Cookies and Web Beacons
Marketing Cookies and web beacons may be set through our website by our advertising partners. They may be used by those companies to build a profile of your interests and show you relevant adverts on other websites. They do not directly store personal information, but uniquely identify your browser and internet device. If you do not allow these cookies and web beacons, you will experience less targeted advertising. Our website does not track users when they cross to third party websites, does not provide targeted advertising to them and therefore does not respond to "Do Not Track" signals.
Allow Marketing Cookies:
Yes
No
Interactive Brokers Group Cookie Policy
What are Cookies and Web Beacons?
Cookies are pieces of data that a website transfers to a user's hard drive for record-keeping purposes. Web beacons are transparent pixel images that are used in collecting information about website usage, e-mail response and tracking. Generally, cookies may contain information about your Internet Protocol ("IP") addresses, the region or general location where your computer or device is accessing the internet, browser type, operating system and other usage information about the website or your usage of our services, including a history of the pages you view.
How We Use Cookies and Web Beacons
Interactive Brokers Group collects information from cookies and web beacons and stores it in an internal database. This information is retained in accordance with our Privacy Policy. This website uses the following cookies and web beacons:
Strictly Necessary Cookies
These cookies are necessary for the website to function and cannot be switched off in our systems. They are usually only set in response to actions made by you which amount to a request for services, such as setting your privacy preferences, logging in or filling in forms. You can configure your browser to block or alert you about these cookies, but certain areas of the site will not function properly. These cookies do not store any personal data.
Performance Cookies and Web Beacons
These cookies and web beacons allow us to count visits and traffic sources so we can measure and improve the performance of our site. They help us to know which pages are the most and least popular and see how visitors move around the site. All information that these cookies and web beacons collect is aggregated and, therefore, anonymous. If you do not allow these cookies and web beacons our aggregated statistics will not have a record of your visit.
 The website uses Google Analytics, a web analytics service provided by Google, Inc. ("Google"). Google Analytics uses cookies to help analyse how you use this website. The information generated by the cookie about your use of this website (including your IP address) will be transmitted to and stored by Google on servers in the United States. Google will use this information for the purposes of evaluating your use of the website, compiling reports on website activity for website operators and providing other services relating to website activity and internet usage. Google may also transfer this information to third parties where required to do so by law, or where such third parties process the information on Google's behalf. Google will not associate your IP address with any other data held by Google.
Functional Cookies
These cookies enable the website to provide enhanced functionality and personalization. They may be set by us or by third party providers whose services we have added to our pages. If you do not allow these cookies, some or all of these services may not function properly.
Marketing Cookies and Web Beacons
These cookies and web beacons may be set throughout our site by our advertising partners. They may be used by those companies to build a profile of your interests and show you relevant advertisements on other sites. They do not store personal information that could identify you directly, but are based on uniquely identifying your browser and internet device. If you do not allow these cookies and web beacons, you will experience less targeted advertising. The website does not track users when they cross to third party websites, does not provide targeted advertising to them and therefore does not respond to Do Not Track ("DNT") signals.
Managing Your Cookie Preferences
You have many choices with regards to the management of cookies on your computer. All major browsers allow you to block or delete cookies from your system. However, if you do decide to disable cookies you may not be able to access some areas of our website or the website may function incorrectly. To learn more about your ability to manage cookies and web beacons and how to disable them, please consult the privacy features in your browser or visit www.allaboutcookies.org.
 This website may link through to third party websites which may also use cookies and web beacons over which we have no control. We recommend that you check the relevant third parties privacy policy for information about any cookies and web beacons that may be used.
Submit
IBKR LLC (U.S.)
Close Navigation
We are redirecting you to the Interactive Brokers LLC (U.S.) Website.
Should you decide to open an account, you will be redirected to the account application for your region. You can also find the website of the IBKR entity for your region at the bottom of this page.
Take me to IBKR.com