"""Labelled test set for :mod:`yuki.memory.secret_filter`.

``POSITIVES``: (text, [(kind, secret), ...]) - every ``secret`` must be gone
from the redacted text, and should be hidden as ``kind``.
``NEGATIVES``: texts that must come back unchanged (hard cases: ids, hashes,
URLs, versions, paths, normal English and Hinglish chat about passwords and
codes).  Every key below is fake: random characters from a seeded generator
in the issuer's shape, or the vendors' documented examples.
"""

from __future__ import annotations

import random

_rng = random.Random(20260925)
_AZ = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_az = _AZ.lower()
_09 = "0123456789"
_B62 = _AZ + _az + _09
_HEX = "0123456789abcdef"


def r(n: int, alphabet: str = _B62) -> str:
    return "".join(_rng.choice(alphabet) for _ in range(n))


def luhn(prefix: str, length: int) -> str:
    """A Luhn-valid number of ``length`` digits starting with ``prefix``."""
    body = prefix + "".join(_rng.choice(_09) for _ in range(length - len(prefix) - 1))
    for check in _09:
        digits = body + check
        total = 0
        for i, ch in enumerate(reversed(digits)):
            d = int(ch) * (2 if i % 2 else 1)
            total += d - 9 if d > 9 else d
        if total % 10 == 0:
            return digits
    raise AssertionError


def group(num: str, sizes: tuple[int, ...], sep: str = " ") -> str:
    out, pos = [], 0
    for s in sizes:
        out.append(num[pos:pos + s])
        pos += s
    return sep.join(out)


AWS = "AKIA" + r(16, _AZ + _09)
ANTHROPIC = "sk-ant-api03-" + r(86, _B62 + "-_") + "AA"
OPENAI = "sk-proj-" + r(48, _B62 + "-_") + "9x"
GOOGLE = "AIza" + r(35, _B62 + "-_")
ELEVEN = "sk_" + r(48, _HEX)
STRIPE = "sk_live_" + r(24)
BEDROCK = "ABSK" + r(60, _B62 + "+/") + "="
GROQ = "gsk_" + r(52)
SENDGRID = "SG." + r(22) + "." + r(43)
GH = "ghp_" + r(36)
GH_PAT = "github_pat_11" + r(20, _AZ + _09) + "_" + r(59)
SLACK = "xoxb-" + r(12, _09) + "-" + r(13, _09) + "-" + r(24)
JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkphbmUifQ." + r(43, _B62 + "-_")
HF = "hf_" + r(34)
NPM = "npm_" + r(36)
GITLAB = "glpat-" + r(20, _B62 + "-_")
TELEGRAM = r(10, "123456789") + ":AA" + r(33, _B62 + "-_")
YA29 = "ya29." + r(60, _B62 + "-_")
HEX32 = r(32, _HEX)
AZURE = r(86, _B62 + "+/") + "=="
PEM = ("-----BEGIN RSA PRIVATE KEY-----\n" + "\n".join(r(64, _B62 + "+/") for _ in range(4))
       + "\n-----END RSA PRIVATE KEY-----")
OPENSSH = ("-----BEGIN OPENSSH PRIVATE KEY-----\n" + "\n".join(r(70, _B62 + "+/") for _ in range(3))
           + "\n-----END OPENSSH PRIVATE KEY-----")
PUTTY = ("PuTTY-User-Key-File-3: ssh-ed25519\nEncryption: none\nComment: key\nPublic-Lines: 2\n" + r(64) + "\n"
         + r(20) + "\nPrivate-Lines: 1\n" + r(44) + "\nPrivate-MAC: " + r(64, _HEX))
VISA = luhn("4", 16)
MC = luhn("53", 16)
AMEX = luhn("37", 15)
RUPAY = luhn("6521", 16)
VISA2 = luhn("45", 16)
SLACK_HOOK = "T" + r(8, _AZ + _09) + "/B" + r(10, _AZ + _09) + "/" + r(24)
DISCORD_HOOK = r(18, _09) + "/" + r(68, _B62 + "-_")
SESSION = r(40, _B62)
SIGNING = r(32, _B62)

POSITIVES: list[tuple[str, list[tuple[str, str]]]] = [
    # -- passwords in credential form (chats, pages, calls) --
    ("pass : Ab9x7788", [("password", "Ab9x7788")]),
    ("user id : 22abcd123@abc.edu.in\npass : Kx7q2209", [("password", "Kx7q2209")]),
    ("id: 21BXYZ042 password: Elina@123", [("password", "Elina@123")]),
    ("username - rahul.k password - sunflower", [("password", "sunflower")]),
    ("login: rahul.k / Tiger@2024", [("password", "Tiger@2024")]),
    ("portal ka id 21BXYZ042 aur pass Elina@123 hai", [("password", "Elina@123")]),
    ("mera password hai Rahul@2001", [("password", "Rahul@2001")]),
    ("iska password Qwerty#77 hai", [("password", "Qwerty#77")]),
    ("the password is Hunter2!x btw", [("password", "Hunter2!x")]),
    ("pwd=S3cr3tP@ss", [("password", "S3cr3tP@ss")]),
    ("DB_PASSWORD=pg_Xx81kq9z", [("password", "pg_Xx81kq9z")]),
    ('{"user": "kian", "password": "b7Kd9!qq"}', [("password", "b7Kd9!qq")]),
    ("Rahul@2001 is the password", [("password", "Rahul@2001")]),
    ("wifi password: chai4life", [("password", "chai4life")]),
    ("the password for the portal is Garden#123", [("password", "Garden#123")]),
    ("creds: admin / Adm1n!2024", [("password", "Adm1n!2024")]),
    ("postgres://app:Zq8vK2mp@db.internal:5432/prod", [("password", "Zq8vK2mp")]),
    ("mongodb+srv://kian:N0tMyRealPass@cluster0.ab12c.mongodb.net/test", [("password", "N0tMyRealPass")]),
    ("Password:\nWinter2026!", [("password", "Winter2026")]),
    ("पासवर्ड है Rahul@123", [("password", "Rahul@123")]),
    ("id pass: 21BXYZ042 Elina@123", [("password", "Elina@123")]),
    ("ATM pin 4821 hai", [("password", "4821")]),
    ("upi pin: 739215", [("password", "739215")]),
    ("pw: kitkat99", [("password", "kitkat99")]),
    ("email: foo@gmail.com, password: Foo#9981", [("password", "Foo#9981")]),
    ("bhai pass bhej raha hu: Mahi@0707", [("password", "Mahi@0707")]),
    ("Your password has been reset. Temporary password: Tmp-4821-xk", [("password", "Tmp-4821-xk")]),
    ("SET PGPASSWORD=Pq7!ab32", [("password", "Pq7!ab32")]),
    ("export MYSQL_PWD='r00t#Pass'", [("password", "r00t#Pass")]),
    ("user / pass: Ramesh / Ram@1990", [("password", "Ram@1990")]),
    ("My Netflix password is Arjun@1998.", [("password", "Arjun@1998")]),
    ("उसका पासवर्ड है kian1234", [("password", "kian1234")]),
    ("login details: elina.m@college.example / Lvu@2024!", [("password", "Lvu@2024")]),
    ("Username: mafex11\nPassword: dragonfly", [("password", "dragonfly")]),
    ("passcode = 90210abc", [("password", "90210abc")]),
    ("redis://:Rk82nvQpz0@cache.local:6379", [("password", "Rk82nvQpz0")]),
    # -- API keys --
    (f"aws key {AWS}", [("api_key", AWS)]),
    (f"ANTHROPIC_API_KEY={ANTHROPIC}", [("api_key", ANTHROPIC)]),
    (f"here's the openai key {OPENAI} use it for the demo", [("api_key", OPENAI)]),
    (f"maps key: {GOOGLE}", [("api_key", GOOGLE)]),
    (f"ElevenLabs {ELEVEN}", [("api_key", ELEVEN)]),
    (f"stripe secret {STRIPE}", [("api_key", STRIPE)]),
    (f"bedrock: {BEDROCK}", [("api_key", BEDROCK)]),
    (f"GROQ_API_KEY=\"{GROQ}\"", [("api_key", GROQ)]),
    (f"sendgrid {SENDGRID}", [("api_key", SENDGRID)]),
    (f"api key: {HEX32}", [("api_key", HEX32)]),
    (f"DefaultEndpointsProtocol=https;AccountName=yukistore;AccountKey={AZURE};EndpointSuffix=core.windows.net",
     [("api_key", AZURE)]),
    ("x-api-key: 7Hq9Lm2Rt8Vw4Zp1Ks6Nd3", [("api_key", "7Hq9Lm2Rt8Vw4Zp1Ks6Nd3")]),
    ("aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", [("api_key", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")]),
    # -- tokens --
    (f"token {GH}", [("token", GH)]),
    (f"GITHUB_TOKEN={GH_PAT}", [("token", GH_PAT)]),
    (f"slack bot {SLACK}", [("token", SLACK)]),
    (f"cookie jwt {JWT}", [("token", JWT)]),
    ("Authorization: Bearer 8f2kQ9zLm3Xv7Rt1Wy6Pn4Hs", [("token", "8f2kQ9zLm3Xv7Rt1Wy6Pn4Hs")]),
    (f"webhook https://hooks.slack.com/services/{SLACK_HOOK}", [("token", SLACK_HOOK)]),
    (f"https://discord.com/api/webhooks/{DISCORD_HOOK}", [("token", DISCORD_HOOK)]),
    (f"hf token {HF}", [("token", HF)]),
    (f"//registry.npmjs.org/:_authToken={NPM}", [("token", NPM)]),
    (f"bot token {TELEGRAM}", [("token", TELEGRAM)]),
    (f"gitlab {GITLAB}", [("token", GITLAB)]),
    (f"access token {YA29}", [("token", YA29)]),
    (f"session token:\n{SESSION}", [("token", SESSION)]),
    (f"https://api.example.com/v1/data?user=5&access_token={SESSION}&page=2", [("token", SESSION)]),
    # -- private keys --
    (f"key file:\n{PEM}\nkeep it safe", [("private_key", PEM)]),
    (OPENSSH, [("private_key", OPENSSH)]),
    (PUTTY, [("private_key", PUTTY)]),
    # -- one-time codes --
    ("Your OTP is 482913. Do not share it with anyone.", [("otp", "482913")]),
    ("482913 is your Amazon OTP. Valid for 10 minutes.", [("otp", "482913")]),
    ("G-731942 is your Google verification code.", [("otp", "731942")]),
    ("Use 559301 to sign in to your Microsoft account", [("otp", "559301")]),
    ("OTP: 7719", [("otp", "7719")]),
    ("bhai OTP aaya 889412 bata de jaldi", [("otp", "889412")]),
    ("verification code: 302 118", [("otp", "302 118")]),
    ("Your login code is 66120", [("otp", "66120")]),
    ("ओटीपी 553201 है", [("otp", "553201")]),
    ("enter code 928374 on the TV", [("otp", "928374")]),
    ("Your Instagram security code is 804211", [("otp", "804211")]),
    # -- payment --
    (f"card {group(VISA, (4, 4, 4, 4))} exp 12/27", [("card", group(VISA, (4, 4, 4, 4)))]),
    (group(MC, (4, 4, 4, 4), "-"), [("card", group(MC, (4, 4, 4, 4), "-"))]),
    (f"my debit card number is {VISA2}", [("card", VISA2)]),
    (f"Amex {group(AMEX, (4, 6, 5))}", [("card", group(AMEX, (4, 6, 5)))]),
    (f"RuPay card {RUPAY}", [("card", RUPAY)]),
    ("CVV 123", [("cvv", "123")]),
    ("cvv: 4321 and expiry 09/28", [("cvv", "4321")]),
    ("A/C no: 50100123456789", [("bank_account", "50100123456789")]),
    ("account number is 1234 5678 9012", [("bank_account", "1234 5678 9012")]),
    ("acc no 918010012345678", [("bank_account", "918010012345678")]),
    ("khata number 30012345678", [("bank_account", "30012345678")]),
    ("IFSC: HDFC0001234", [("ifsc", "HDFC0001234")]),
    ("IFSC code SBIN0012345", [("ifsc", "SBIN0012345")]),
    ("A/C 50100123456789 HDFC0001234 Kian", [("bank_account", "50100123456789"), ("ifsc", "HDFC0001234")]),
    # -- other secrets --
    ("client_secret: 8Jd9Kq2Lp4Mz7Nx1Qw5Er3Ty", [("secret", "8Jd9Kq2Lp4Mz7Nx1Qw5Er3Ty")]),
    ("whsec_" + SIGNING, [("secret", "whsec_" + SIGNING)]),
    ("GOCSPX-" + r(28, _B62 + "-_"), [("secret", None)]),
    (f"here's the webhook signing secret\n{SIGNING}", [("secret", SIGNING)]),
]
# a None secret means "the whole text after the label": resolve it
POSITIVES = [(t, [(k, s if s is not None else t) for k, s in exp]) for t, exp in POSITIVES]

#: Secrets that only a label or a word next to them identifies ("pass : X", "OTP 482913", "api key: <hex>",
#: "A/C no: ..."): out of the filter's scope since the owner's decision of 2026-09-25 (docs/MEMORY.md
#: "Secret filter", option B: fixed formats only). A password counts only inside a URL's userinfo.
LABEL_ONLY: frozenset[str] = frozenset(
    {s for t, exp in POSITIVES for k, s in exp
     if k in ("otp", "cvv", "bank_account") or (k == "password" and "://" not in t)}
    | {HEX32, AZURE, "7Hq9Lm2Rt8Vw4Zp1Ks6Nd3", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", SESSION,
       "8Jd9Kq2Lp4Mz7Nx1Qw5Er3Ty", SIGNING}
)
#: Hard negatives the fixed-format filter hides anyway: 4111111111111111 is the canonical Visa test number,
#: Luhn-valid in a Visa IIN range - a card number by format, and no word may say otherwise.
ACCEPTED_FALSE_POSITIVES: frozenset[str] = frozenset({"Invoice no 4111111111111111 dated 2026-09-25"})

NEGATIVES: list[str] = [
    # ids and hashes
    "request id 550e8400-e29b-41d4-a716-446655440000 failed",
    "session 3f2504e0-4f89-11d3-9a0c-0305e82c3301 started",
    "commit 9fceb02d0ae598e95dc970b74767f19372d61af8",
    "merged a1b2c3d into main",
    "git checkout 4e1243bd22c66e76c2ba9eddc1f91394e57f9f83",
    "sha256: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "md5 d41d8cd98f00b204e9800998ecf8427e",
    "Discord user 123456789012345678 joined",
    # hashes and ids in URLs
    "https://cdn.example.com/assets/app.3f9a7c1e5b2d4f6a8c0e.js",
    "https://github.com/mafex11/yuki/commit/9fceb02d0ae598e95dc970b74767f19372d61af8",
    "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg?sqp=-oaymwEcCNACELwBSFXyq4qpAw4IARUAAIhCGAFwAcABBg==&rs=AOn4CLBx2kQ9",
    "https://docs.google.com/document/d/1aBcD3fGhIjKlMnOpQrStUvWxYz0123456789AbCdE/edit",
    "https://drive.google.com/file/d/1Xy9ZkQ2mN8pL4vR7tW3sB6hJ0cF5gD1a/view?usp=sharing",
    "https://www.notion.so/Roadmap-8f2c4a1e9b7d4c3a8e6f1b2d3c4e5f6a",
    "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC?si=1f2e3d4c5b6a7980",
    "https://www.amazon.in/dp/B0CHX3QBCH/ref=sr_1_3?crid=2M8YQ4ZTX1V7K&keywords=headphones",
    # base64 images
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==",
    '<img src="data:image/jpeg;base64,/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/">',
    # versions
    "Python 3.13.0 (tags/v3.13.0:60403a5, Oct  7 2024)",
    "v2.14.1-rc.3",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/129.0.6668.90 Safari/537.36",
    "node v20.11.1 npm 10.2.4",
    "faster-whisper==1.1.0 ctranslate2==4.5.0",
    # file paths
    r"C:\Users\yukidev\yuki\yuki\memory\secret_filter.py",
    "/usr/local/lib/python3.13/site-packages/token_utils/__init__.py",
    "src/components/AuthTokenProvider.tsx",
    r"%LOCALAPPDATA%\Yuki\memory\memory.key",
    # YouTube ids
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "youtu.be/9bZkp7q19f0",
    "https://www.youtube.com/watch?v=kJQP7kiw5Fk&list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG&index=3",
    "Watch later: YOU WERE THE ONE (dQw4w9WgXcQ) by quix",
    # order and other numbers
    "Order #402-1234567-8901234 has shipped",
    "Your order 171-9384756-2837465 will arrive Friday",
    "Order ID: OD428193745612389100",
    "Flipkart order OD1234567890123456 delivered",
    "tracking number 1Z999AA10123456784",
    "PNR 8423917650, coach S4 berth 32",
    "Transaction ID 412345678901 successful",
    "UTR 123456789012 credited",
    "Invoice no 4111111111111111 dated 2026-09-25",
    "call me at +91 98765 43210",
    "total ₹1,23,456.00 paid",
    "Card ending in 4242",
    "account 12345 settings",
    "12/25/2026 10:30 AM",
    "2026-09-25T10:11:12Z",
    # Hinglish chat
    "bhai kal exam hai pass ho jaunga",
    "password bhool gaya yaar, reset karna padega",
    "OTP nahi aaya abhi tak",
    "pass kar de bhai notes",
    "code bhej de jo tune likha tha",
    "card se payment kar diya",
    "account bana liya maine",
    "kal 10 baje milte hain",
    "mera pass time nahi hai abhi",
    "tera password strong hona chahiye",
    "pin kar de message ko",
    "bus pass renew karna hai",
    "id / pass kya hai?",
    "IFSC code kya hai bank ka?",
    "मेरा पासवर्ड भूल गया",
    "पासवर्ड बदल दिया",
    # English about passwords and codes
    "I forgot my password again",
    "Reset your password using the link below",
    "Password must be at least 8 characters long",
    "Password: Must contain a number and a symbol",
    "Enter your password",
    "Your password was changed on Sep 24",
    "The passcode feature is great",
    "pass the salt please",
    "She passed the exam with 92%",
    "code 404 not found",
    "Error code 1004",
    "zip code 94107",
    "Pin code 560001",
    "Pin: 560001, Eastvale",
    "The meeting code is in the invite",
    "I'll send the code tomorrow",
    "Your code review is due",
    "code: 2026 roadmap",
    "Pass: 2 adults, 1 child",
    "Passengers: 3",
    "Key: Value",
    "Token: 5 remaining",
    "api-key header missing",
    "The API key is in the dashboard",
    "rotate the token every week",
    "Bearer tokens expire after an hour",
    "sk-learn is not a package, scikit-learn is",
    "login / signup",
    "user/pass authentication is deprecated",
    "TCP/IP and/or 50/50 24/7",
    # code that names secrets without holding one
    "password = os.environ['DB_PASSWORD']",
    "token = get_token()",
    "api_key: ${OPENAI_API_KEY}",
    "const password = req.body.password;",
    "if (password.length < 8) return false;",
    "max_tokens=4096",
    "token_count: 1534",
    "passwordless: true",
    '"password": "<your password>"',
    "PASSWORD=changeme",
    "password: ********",
    "pass : [password hidden]",
    # long random-looking tokens without a secret context
    "Build 20260925.3 artifact yuki-win-x64-8f2c4a1e9b7d4c3a.zip",
    "css class jsx-3f9a7c1e5b2d4f6a8c0e sc-bdVaJa-kLmNoP",
]
