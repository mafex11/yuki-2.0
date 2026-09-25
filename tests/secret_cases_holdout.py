"""Blind held-out cases for the Yuki secret filter.

Written without looking at secret_filter.py or secret_cases.py. All keys and
credentials here are FAKE: random characters shaped like the real formats.
Card numbers are built with a Luhn helper so they pass the checksum.

HOLDOUT_POSITIVES: (text, [(kind, secret_substring), ...])
HOLDOUT_NEGATIVES: texts with no secret that a naive filter might flag.
"""


def _luhn_complete(partial: str) -> str:
    """Append the Luhn check digit to `partial` (digits only)."""
    total = 0
    for i, ch in enumerate(reversed(partial)):
        d = int(ch)
        if i % 2 == 0:  # these become the doubled positions once the check digit is appended
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return partial + str((10 - total % 10) % 10)


def _group(num: str, sizes, sep: str = " ") -> str:
    out, pos = [], 0
    for n in sizes:
        out.append(num[pos:pos + n])
        pos += n
    return sep.join(out)


# Luhn-valid, fake card numbers
_VISA = _luhn_complete("421653007941825")          # 16 digits
_MC = _luhn_complete("530127649083117")            # 16 digits
_RUPAY = _luhn_complete("608326114509273")         # 16 digits
_AMEX = _luhn_complete("37649105827361")           # 15 digits
_VISA2 = _luhn_complete("438628190562734")         # 16 digits

VISA_SPACED = _group(_VISA, (4, 4, 4, 4))
MC_PLAIN = _MC
RUPAY_SPACED = _group(_RUPAY, (4, 4, 4, 4))
AMEX_SPACED = _group(_AMEX, (4, 6, 5))
VISA2_DASHED = _group(_VISA2, (4, 4, 4, 4), "-")

# Fake keys of realistic shape
_AWS_ID = "AKIAVI6H1P5ZF7QASAVP"
_AWS_ID2 = "AKIAO3AHM6Q0UMOO152B"
_AWS_SECRET = "jlK6tKx62Ur3ZU+gepvfpTNB4KkLj4D8C+WO4wyL"
_ANT = ("sk-ant-api03-TgtU3gpDZEAq1o0zpRTgo2sQpvzCD1dCFuk_kpRe2UUJs69wnmOZkn1WSl28b"
        "HLudIgxTW0P6gAro_Yjz0-j_kPqZ8mOZAA")
_PROJ = ("sk-proj-1e_LFeHPeOut-Q9nPj0BztJhv57nNSOeuU2KfElKrdamWGQjyl_6WByloMz-mhORfqoSe1hnxS"
         "T3BlbkFJfw7n4YcfGfnghi1s25USKDGmFjx7p8oz1U9nmgIvHztJbUffX3qvUaKkxOXNEa_ukNBDI7adSA")
_GHP = "ghp_hbtTE1junVEDs7WYVGZwGloPoClrG1UHMtdd"
_GH_PAT = "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"
_XOXB = "xoxb-8435546670-1194638344012-c06nAnk1ZtvUiQhVysyyDbyI"
_AIZA = "AIzaSyK33qOxvgaHyyfTsP2GhfcR298NeQ3DqhZ"
_AIZA2 = "AIzaSyS7Y4xU60t-C5XZDwwjlqh1xXS-6w9VfIE"
_STRIPE = ("sk_live_51DLpl2pXQhPPHPdk9xRRGWsbP6EwUke8sMtE6ieeDhxCW06hHiJgV2JSpG3eWlYEfMETt4Ln2b73"
           "ooqglv3KPwAKvDRW2rTsLy")
_WHSEC = "whsec_zfZsKaXjxnnEGX079r6m7mtppBLVYzlk"
_HF = "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"
_GSK = "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"
_NPM = "npm_jQ79IOKuTxGyg05VbYtFyNOEi2YyYJ3qQ7He"
_SG = "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"
_JWT = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ1Xzg4MTIiLCJuYW1lIjoiQXJqdW4gTSIsImlhdCI6"
        "MTc1ODc5MjAwMCwiZXhwIjoxNzU4ODc4NDAwfQ.JrOW3ErGKoWAQNlkHT9ocVCA3SAXpAqQjReueAzLrY-")
_JWT2 = ("eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VyX2lkIjo0NDcxLCJyb2xlIjoiYWRtaW4iLCJpYXQiOjE3"
         "NTg4MDEyMzR9.x8-_J5iAmEgJCoOu7XnPbOMNEvRxBkG2pwUfqJm_JzLL18u-K_4uME84axEwja07ZVF50Tuh-TAiwf2o5AO1B7")
_DISCORD_BOT = "MTA5ODc2NTQzMjEwOTg3NjU0Mw.jwZTpV.4wsEWh3iFYOjEVP3yh0zBhX50SMT5I5hjSr97N"
_DJANGO = "gi&2*9r2*3)w0kf6r2-p2^kajkvlwvrl!(-6$())pp*y$e8_i%"
_RZP_SECRET = "3QSDpcT6zE4nfV0aUaZbWuNR"
_GOOGLE_CLIENT_SECRET = "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"

_PEM_RSA = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "NNhMRwgOBDv2sCWcni47jvYGpYD6YS4eK+ukwuhvZtClDes7UgTWM6dAJ++hjA4P\n"
    "NtnPqRQVkF8FbteKCQNfez+BrM2VZycJ8/BuZC8COSjtAygatUW+O1n4Kqfx5X6Y\n"
    "6G3VZLrW0gbFuJwjxEkuk+9Ts4hBVU0o151rSAtt1Qavhh3fvhWPnC7kEGggMHJv\n"
    "2GB8feLjUnBvHl/0gW9rsyqVOfZ9/QQeFy/dAq5UrXyGSvhL4A4Pab5Sjn3FsJdu\n"
    "RZ2wPJJRx3FgtrBZQVom4HfxY3aadUdTJRZ+miNtbXCO46dI4uVZ3f9Al/3w0rOa\n"
    "hbZDPE+diER4us1dWTsiqFni6wi7uj0x7ZaNdWwxwWtHFkqJb6b07Fi7c+xU066w\n"
    "7ReXjnYsoysC7Fci6XwiQC3/jbFh53ten3G6pDGW==\n"
    "-----END RSA PRIVATE KEY-----"
)
_PEM_OPENSSH = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n"
    "TwxLGLfVdOwM/UUkmTR1u/NKXKtOUmFKRoxlE0Jl/mVvYdQ8qY76M5y8i3pbdJbqvlLP8r\n"
    "u7vJg04+5xXfVTJxtSiTv0SBeGIfCow6koEJGIyUMbxitHsywQcV4T2RnA+gLnxYGmeDAG\n"
    "32WvOVFJ3i2s9bSqbyVwdSyGdJMgGUMYTgb+e9uFRh1zoLLGMDQtZ4DlcmSQLgoDAVey2L\n"
    "GCwOD8eby8qOs1Tt2hxowE2SEhAr5D7jdB76auhjsqUNEW0rBnmmDo0tL91RoDJ9K2tfV/\n"
    "T1XEmrvXqgqX96JVfgG0iEuqSUJ/H+1M\n"
    "-----END OPENSSH PRIVATE KEY-----"
)
# Private key as it appears inside a GCP service-account JSON (literal "\n" escapes)
_PEM_JSON = (
    "-----BEGIN PRIVATE KEY-----\\n"
    "HXzOrAiCIUlSlkUGr+NhK5ajKCSNT6LzCqQWrDtPNBxQkw3t09fjm+Xo9tiOw2Px\\n"
    "VCoifLGmBz+FY6ftxZ8MSVPDKDmPtlfjWZCgh8aG8BuMvPITH5OLK+bQdkY+sxZa\\n"
    "PCPI3mcgMbE8mqLVtuc/gO0VhejFWKSGIl/kuFcSdYYopaC4IFtDPicjeCnriBNx\\n"
    "2eZhtI+eYBY3LzTGrfWJEtfd+CByBkypfmzXjtGo4mHCNAuXkFAo4r+XwnIR5jPF\\n"
    "/ulUssccfvFlGUEq5aRB2uRhVn00=\\n"
    "-----END PRIVATE KEY-----\\n"
)


HOLDOUT_POSITIVES: list[tuple[str, list[tuple[str, str]]]] = [
    # ---------------- passwords: chat, Hinglish, Hindi, typos ----------------
    ("bro netflix ka login le le\nrahul.k99@gmail.com\npass: Chennai@2024\nprofile 'Rahul' pe mat dekhna kuch",
     [("password", "Chennai@2024")]),
    ("wifi ka pwd hai kaveri#1234 , router ke peeche bhi likha hai",
     [("password", "kaveri#1234")]),
    ("my paswrd is tH3m00n!! dont tell anyone lol",
     [("password", "tH3m00n!!")]),
    ("mera insta ka password Riya@1405 hai, story daal dena kal meri taraf se",
     [("password", "Riya@1405")]),
    ("मेरा पासवर्ड है Sanjay#789 किसी को मत बताना",
     [("password", "Sanjay#789")]),
    ("Hi Arjun,\n\nYour account on the Corvana VPN portal has been created.\n"
     "Username: arjun.m\nTemporary password: Xk7#pQ2mLw\n\n"
     "You will be asked to change this password after your first login.\n\nRegards,\nIT Helpdesk",
     [("password", "Xk7#pQ2mLw")]),
    ("VTOP login\nLogin ID: 21BCE1043\nPwd: vit@Chennai21\nattendance check karke bata",
     [("password", "vit@Chennai21")]),
    ("[00:14:32] haan so my password is bluewhale2002, all small letters, no spaces",
     [("password", "bluewhale2002")]),
    ("[00:03:11] तो पासवर्ड है tiger1998 सब छोटे अक्षर में, ठीक है?",
     [("password", "tiger1998")]),
    ("@here staging admin creds for the demo -> user: admin / pass: St@g1ng!Blr  (rotate after friday pls)",
     [("password", "St@g1ng!Blr")]),
    ("gmail ka password bhool gaya tha, naya wala hai Pooja$2026 note kar le",
     [("password", "Pooja$2026")]),
    ("psst my steam acc is kabir_op and passwd is ValorantGod!7 , just dont change the email",
     [("password", "ValorantGod!7")]),
    ("WiFi name: Airtel_Sharma_5G\nPassword: sharma@home123",
     [("password", "sharma@home123")]),
    ("college portal pw - Anna#univ2025, results aaye toh screenshot bhej",
     [("password", "Anna#univ2025")]),
    ("PS C:\\Users\\yukidev\\proj> mysql -u root -pR00t@local123 -h 127.0.0.1 yuki_dev",
     [("password", "R00t@local123")]),
    ("$ docker login -u sneha -p Dock3r!Pass2 registry.gitlab.com",
     [("password", "Dock3r!Pass2")]),
    ("hotstar ka id pass: amma.hotstar@gmail.com / Lakshmi@1971 , family plan hai",
     [("password", "Lakshmi@1971")]),
    ("[00:22:05] uh the password is capital M, mango, underscore, then 42, so Mango_42 basically",
     [("password", "Mango_42")]),

    # ---------------- .env lines / connection strings ----------------
    ("DB_HOST=localhost\nDB_USER=yuki\nDB_PASSWORD=pg_s3cr3t_Pa55\nDB_NAME=yuki_prod",
     [("password", "pg_s3cr3t_Pa55")]),
    ("DATABASE_URL=postgres://yuki_app:Qw9vLm2xTz@db.internal.blr:5432/prod",
     [("password", "Qw9vLm2xTz")]),
    ("client = MongoClient(\"mongodb+srv://karthik:K4rth1k%40mdb@cluster0.ab1cd.mongodb.net/test?retryWrites=true\")",
     [("password", "K4rth1k%40mdb")]),
    ("REDIS_URL=redis://default:aZ8kLq3Vt0Rp@redis-12345.c1.ap-south-1.ec2.cloud.redislabs.com:12345",
     [("password", "aZ8kLq3Vt0Rp")]),
    (f"ANTHROPIC_API_KEY={_ANT}\nMODEL=claude-haiku",
     [("api_key", _ANT)]),
    (f"# .env.local\nOPENAI_API_KEY=\"{_PROJ}\"\nNEXT_PUBLIC_APP_URL=http://localhost:3000",
     [("api_key", _PROJ)]),
    (f"AWS_ACCESS_KEY_ID={_AWS_ID}\nAWS_SECRET_ACCESS_KEY={_AWS_SECRET}\nAWS_DEFAULT_REGION=ap-south-1",
     [("api_key", _AWS_ID), ("secret", _AWS_SECRET)]),
    (f"RAZORPAY_KEY_ID=rzp_test_Kf82LmQx0Pz3Ab\nRAZORPAY_KEY_SECRET={_RZP_SECRET}",
     [("secret", _RZP_SECRET)]),
    (f"SECRET_KEY = '{_DJANGO}'\nDEBUG = False\nALLOWED_HOSTS = ['yuki.local']",
     [("secret", _DJANGO)]),
    (f"GOOGLE_CLIENT_ID=812736450912-q8f3kd0s7lm2n4p6r9t1v5x7z0b3c6e8.apps.googleusercontent.com\n"
     f"GOOGLE_CLIENT_SECRET={_GOOGLE_CLIENT_SECRET}",
     [("secret", _GOOGLE_CLIENT_SECRET)]),
    (f"STRIPE_SECRET_KEY={_STRIPE}\nSTRIPE_WEBHOOK_SECRET={_WHSEC}",
     [("api_key", _STRIPE), ("secret", _WHSEC)]),

    # ---------------- API keys / tokens in chat, code, terminal ----------------
    (f"bhai yeh le mera groq key, rate limit mat khatam karna: {_GSK}",
     [("api_key", _GSK)]),
    (f"curl https://api.openai.com/v1/chat/completions -H \"Content-Type: application/json\" "
     f"-H \"Authorization: Bearer {_PROJ}\" -d '{{\"model\":\"gpt-4o-mini\"}}'",
     [("api_key", _PROJ)]),
    (f"curl -H \"Authorization: token {_GHP}\" https://api.github.com/user/repos",
     [("token", _GHP)]),
    (f"git remote set-url origin https://{_GH_PAT}@github.com/yukidev/yuki.git",
     [("token", _GH_PAT)]),
    (f"slack_client = WebClient(token=\"{_XOXB}\")",
     [("token", _XOXB)]),
    (f"<script src=\"https://maps.googleapis.com/maps/api/js?key={_AIZA}&callback=initMap\"></script>",
     [("api_key", _AIZA)]),
    (f"firebaseConfig = {{ apiKey: \"{_AIZA2}\", authDomain: \"canteen-app-7f2c1.firebaseapp.com\" }}",
     [("api_key", _AIZA2)]),
    (f"from huggingface_hub import login\nlogin(token=\"{_HF}\")",
     [("api_key", _HF)]),
    (f"yo use my HF token for the whisper download {_HF} , delete after",
     [("api_key", _HF)]),
    (f"//registry.npmjs.org/:_authToken={_NPM}",
     [("token", _NPM)]),
    (f"sg = SendGridAPIClient('{_SG}')",
     [("api_key", _SG)]),
    (f"DISCORD_BOT_TOKEN={_DISCORD_BOT}",
     [("token", _DISCORD_BOT)]),
    (f"GET /api/v1/me HTTP/1.1\nHost: api.canteen.in\nAuthorization: Bearer {_JWT}\nAccept: */*",
     [("token", _JWT)]),
    (f"localStorage.getItem('access_token') -> \"{_JWT2}\"",
     [("token", _JWT2)]),
    (f"aws configure\nAWS Access Key ID [None]: {_AWS_ID2}\nAWS Secret Access Key [None]: {_AWS_SECRET}",
     [("api_key", _AWS_ID2), ("secret", _AWS_SECRET)]),

    # ---------------- private keys ----------------
    (f"$ cat ~/.ssh/id_rsa\n{_PEM_RSA}\n$ ",
     [("private_key", _PEM_RSA)]),
    (f"bhai deploy key yeh hai, server pe daal de\n{_PEM_OPENSSH}",
     [("private_key", _PEM_OPENSSH)]),
    ("{\n  \"type\": \"service_account\",\n  \"project_id\": \"yuki-stt-4471\",\n"
     f"  \"private_key_id\": \"a1f93c0e5b7d2468ace013579bdf2468ace01357\",\n  \"private_key\": \"{_PEM_JSON}\",\n"
     "  \"client_email\": \"stt@yuki-stt-4471.iam.gserviceaccount.com\"\n}",
     [("private_key", _PEM_JSON)]),

    # ---------------- OTPs / 2FA ----------------
    ("836214 is OTP for txn of INR 2,499.00 at SWIGGY on HDFC Bank card ending 4417. Valid till 14:32. "
     "Do not share OTP for security reasons. -HDFC Bank",
     [("otp", "836214")]),
    ("Your Zomato verification code is 482913. Do not share this code with anyone.",
     [("otp", "482913")]),
    ("Dear Customer, 739204 is the OTP to login to your PhonePe account. Never share OTP with anyone "
     "incl. PhonePe employees.",
     [("otp", "739204")]),
    ("Use OTP 551802 to log in to your Paytm account. Paytm never calls you asking for OTP. Sharing it "
     "with anyone gives them full access to your Paytm account.",
     [("otp", "551802")]),
    ("G-583920 is your Google verification code.",
     [("otp", "583920")]),
    ("bhai otp aaya 847261 jaldi daal, 2 min mein expire hoga",
     [("otp", "847261")]),
    ("आपका OTP 318475 है। इसे किसी के साथ साझा न करें। -SBI",
     [("otp", "318475")]),
    ("OTP for your transaction of Rs.1,200.00 at AMAZON PAY on ICICI Bank Credit Card XX9012 is 604917. "
     "OTP valid for 10 mins. Do not share. -ICICI Bank",
     [("otp", "604917")]),
    ("Discord login - enter your authenticator code\n> 392 817",
     [("otp", "392 817")]),
    ("[00:05:40] haan ruko, code aaya hai... two factor code is 7 1 9 3 0 4",
     [("otp", "7 1 9 3 0 4")]),

    # ---------------- cards / CVV ----------------
    (f"bhai mera card use kar le booking ke liye: {VISA_SPACED} exp 08/29 cvv 417",
     [("card", VISA_SPACED), ("cvv", "417")]),
    (f"Card number\n{MC_PLAIN}\nExpiry (MM/YY)\n11/28\nCVV\n902\nName on card\nMEERA IYER",
     [("card", MC_PLAIN), ("cvv", "902")]),
    (f"[00:31:12] so my card number is {RUPAY_SPACED} and the expiry is oh nine twenty seven",
     [("card", RUPAY_SPACED)]),
    (f"papa ka amex: {AMEX_SPACED}, CID 7314, sirf flight ke liye use karna",
     [("card", AMEX_SPACED), ("cvv", "7314")]),
    (f"card no {VISA2_DASHED} hai, OTP aayega toh bata dunga",
     [("card", VISA2_DASHED)]),

    # ---------------- bank account / IFSC ----------------
    ("rent ke liye NEFT kar de\nName: Priya Subramanian\nA/c no: 50100234567891\nIFSC: HDFC0001234\nBranch: Adyar, Chennai",
     [("bank_account", "50100234567891"), ("ifsc", "HDFC0001234")]),
    ("fees yahan bhejna - SBI account 32817465093, IFSC SBIN0004281 (Koramangala branch), naam Rohit Kumar",
     [("bank_account", "32817465093"), ("ifsc", "SBIN0004281")]),
    ("मेरा अकाउंट नंबर 004801562378 है और IFSC ICIC0000048, पैसे आज ही डाल देना",
     [("bank_account", "004801562378"), ("ifsc", "ICIC0000048")]),
]


#: Secrets only a label or a word next to them identifies: out of the filter's scope since 2026-09-25
#: (docs/MEMORY.md "Secret filter", option B: fixed formats only). Added after the set was written.
HOLDOUT_LABEL_ONLY: frozenset[str] = frozenset(
    {s for t, exp in HOLDOUT_POSITIVES for k, s in exp
     if k in ("otp", "cvv", "bank_account") or (k == "password" and "://" not in t)}
    | {_AWS_SECRET, _RZP_SECRET, _DJANGO}
)


HOLDOUT_NEGATIVES: list[str] = [
    # chat about credentials without values
    "otp aaya kya? mera abhi tak nahi aaya, network hi nahi hai hostel mein",
    "password change kar diya maine, ab login karke dekh",
    "bhai forgot password pe click kar, reset link aayega mail pe",
    "tera password kya hai? mat bata actually, main khud reset kar dunga",
    "मैंने पासवर्ड बदल दिया है, अब नया वाला याद रखना पड़ेगा",
    "हाँ OTP आ गया, मैंने डाल दिया, अब हो गया",
    "OTP 5 min mein expire ho jayega, jaldi kar",
    "wifi ka naam Airtel_Sharma_5G hai, password wahi purana wala",
    "api key .env mein daal, commit mat karna warna GitHub pe leak ho jayega",
    "Password must be at least 8 characters and include 1 uppercase letter, 1 number and 1 symbol.",
    "Password: ••••••••••  [Show]",
    "Enter the 6-digit code we sent to +91 ******4321",
    "Never share your OTP, PIN or CVV with anyone. HDFC Bank never asks for these details.",
    "IFSC code kahan milega? passbook ke first page pe hota hai",
    "[00:12:08] yeah I'll send you the password later on WhatsApp, not on the call",
    # code referencing env vars / placeholders
    "api_key = os.getenv(\"ANTHROPIC_API_KEY\")\nclient = anthropic.Anthropic(api_key=api_key)",
    "const token = process.env.GITHUB_TOKEN;\nif (!token) throw new Error('GITHUB_TOKEN missing');",
    "OPENAI_API_KEY=your-api-key-here\nDB_PASSWORD=<your-db-password>",
    "password = getpass.getpass(\"Enter password: \")",
    "JWT has three parts: header.payload.signature -> jwt.decode(token, key, algorithms=['HS256'])",
    # ids, hashes, urls
    "Request ID: 3f2b8c1e-9d4a-4b7e-a1c2-7e5f6d8a9b0c",
    "commit f80033c20c5fc18eceb77c3476b7e0b31e467a7b\nAuthor: yukidev <yukidev@users.noreply.github.com>\n\n    fix: stt deferral when game running",
    "Merge pull request #142 from yukidev/feature/JIRA-4821-fix-login (4e8d2a1)",
    "SHA256: 5b039552e8bf5136bddcb8f97c5dc219f096b6385e8d4c9ae136da3147d8dd66  whisper-small.bin",
    "sha256:a3ed95caeb02ffe68cdd9fd84406680ae93d633cb16422d00e8a7c22955b46d4",
    "https://drive.google.com/file/d/1aB2cD3eF4gH5iJ6kL7mN8oP9qR0sT1uV/view?usp=sharing",
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG&t=42s",
    "https://www.notion.so/corvana/Sprint-Planning-8a1f3c5e7b9d4f2a6c8e0b1d3f5a7c9e",
    "https://www.amazon.in/dp/B0CHX2F5QT?ref_=cm_sw_r_apan_dp_7KQ9ZP1M3XJ4VN8TR2W6&th=1",
    "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC?si=8f2c1a9b3d7e4f60",
    "<img src=\"data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==\">",
    "//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJzb3VyY2VzIjpbImFwcC50cyJdLCJtYXBwaW5ncyI6IkFBQUEifQ==",
    "\"node_modules/react\": {\n  \"version\": \"18.3.1\",\n  \"integrity\": \"sha512-aHmkdR1kAPDP4G/Ko/1f6s2KYW7JPYcwlt0jpYpwSt58M8cBYyFYy21PzABTWh8jVY8uu9B0bNn/fHE+vBqGcg==\"\n}",
    "<div class=\"css-1q2w3e4 sc-bdVaJa kWrXJy _3xT9aB_7kQ\">",
    "ETag: \"33a64df551425fcc55e4d42a148795d9f25f89d4\"",
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIkHSwppX3gVc1WtYHdbzpNj7yyuYXBpvN2PxJzsMCYGy yukidev@DESKTOP-7Q2K",
    "<@482937561028374651> gg bro, clutch tha woh",
    "Discord User ID: 1029384756102938475 (copied from Developer Mode)",
    # versions, paths, logs, errors
    "Python 3.12.4 | node v20.11.1 | torch 2.3.0+cu121 | CUDA 12.1",
    "numpy==1.26.4\nrequests>=2.31.0\nfaster-whisper==1.0.3",
    "C:\\Users\\yukidev\\AppData\\Local\\Programs\\Python\\Python312\\python.exe",
    "/home/ubuntu/.cache/huggingface/hub/models--Systran--faster-whisper-small/snapshots/536b0662742c02347bc0e980a01041f333bce120/model.bin",
    "2026-09-25T14:32:07.118Z [INFO] server started on port 8080 (pid 18244)",
    "Error 0x80070005: Access is denied.",
    "net::ERR_CONNECTION_TIMED_OUT  |  HTTP 403 Forbidden  |  code 404 aa raha hai bhai",
    "192.168.1.1 is the default gateway; MAC 3C:52:82:1A:9F:07",
    # orders, tracking, transactions, phone numbers
    "Your Swiggy order #172839465012 from Meghana Foods has been delivered. Enjoy your meal!",
    "Delhivery AWB 1490218734562: out for delivery, expected by 7 PM",
    "PNR: 4521896307 | 12658 Chennai Mail | Coach S4, Berth 32 | CNF",
    "UPI transaction successful. Rs 450.00 paid to Ramesh Tea Stall. UTR: 426719083354",
    "A/c XX4417 debited by Rs 1,250.00 on 24-09-26 for UPI/P2M/426719083354. Avl Bal Rs 12,480.55 -HDFC Bank",
    "call me on 98450 12345 or +91 91234 56789 after 6",
    "reference no: 7302 5519 8846, keep it for future correspondence",
    "Flat 302, Prestige Shantiniketan, Whitefield, Eastvale 560048",
    "Invoice No: INV-2026-004417 dated 12/09/2026 | Amount: Rs 3,540.00 incl. GST",
    "Zoom meeting ID: 845 2217 9063, join 5 min early",
    "Reg No 21BCE1043, CGPA 8.74, placement drive on 3 Oct",
    # cricket, prices, misc
    "IND 287/4 (42.3 ov) Kohli 104*(98), Rahul 56(61) | CRR 6.75",
    "iPhone 16 at ₹79,900, flat ₹5,000 off with HDFC cards till Sunday",
    "use code SWIGGYIT for 60% off up to ₹120 on your next order",
    "=VLOOKUP(A2,Sheet2!$A$1:$D$500,4,FALSE)",
]
