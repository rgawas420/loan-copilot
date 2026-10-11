"""Conversational messages ("hi", "thanks", "who are you?") answered directly, without searching the knowledge base.

A message takes this route only when every part of it is small talk and it names no bank, product, loan fact,
number or code, so "hi, what's the Axis HL code?" still goes through retrieval. The reply is a fixed text (it
mirrors the greeting: "Good morning!", "Namaste!"): instant, free, and it can never state a lender fact. An LLM
call would add a 2-4 s network round trip to Groq for a one-line reply.
"""
import re
from typing import Iterator, List, Optional

from app.rag import entities

_FILLER = r"(?:\s+(?:there|bro|sir|madam|mam|team|buddy|dear|ji|again|so much|very much|a lot|copilot|bot|all|everyone|guys))*"
KINDS = {
    "greeting": r"(?:hi+|hello+|hey+|hlo|helo|hai|namaste|namaskar|hola|yo|greetings|good (?:morning|afternoon|evening|day))",
    "how_are_you": r"(?:how are you(?: doing)?|how r u|how are u|hows it going|how is it going|what'?s up|wassup|sup)",
    "thanks": r"(?:thanks?|thank you|thank u|thx|ty|tysm|much appreciated|appreciate it|great|awesome|nice|cool|perfect|"
              r"super|excellent|good|ok+|okay|okk|k|got it|understood|fine|done|sure|noted|alright)",
    "bye": r"(?:bye+|goodbye|good bye|see you|see ya|take care|good night|gn|catch you later|that'?s all)",
    "about": r"(?:who are you|what are you|what is this|what can you do|what do you do|how can you help(?: me)?|"
             r"how do (?:i|you) use (?:this|you)|how does this work|help|help me|what can i ask|test|testing)",
}
# A part is one or more small-talk phrases in a row ("ok thanks", "ok great bye"), each with optional fillers.
_ANY = "|".join(KINDS.values())
_PART = re.compile(rf"^(?:(?:{_ANY}){_FILLER}(?:\s+|$))+$")
_FIRST = {k: re.compile(rf"^(?:{v})(?=\s|$)") for k, v in KINDS.items()}

FALLBACK = {
    "greeting": "Hi! How can I help you today? Ask me about lender policies, DSA codes, RM contacts, payouts or pincode serviceability, or describe a loan case to find suitable lenders.",
    "how_are_you": "I'm doing well, thanks! What can I help you with today?",
    "thanks": "You're welcome! Let me know if there's anything else you need.",
    "bye": "Goodbye! Come back any time you need help with a loan case.",
    "about": "I'm the Urban Money Loan Copilot. I answer questions from Urban Money's knowledge base (lender policies, DSA codes, RM contacts, payout sheets, pincode lists) and help you find suitable lenders for a loan case. Just ask, or describe a case.",
}

def kind(message: str, attachments=()) -> Optional[str]:
    """The kind of small talk ('greeting', 'thanks', ...), or None when the message needs the knowledge base."""
    if attachments:
        return None
    text = re.sub(r"\s+", " ", (message or "").lower().replace("’", "'")).strip()
    if not text or len(text) > 80:
        return None
    if entities.find_banks(text) or entities.find_products(text) or entities.salient_terms(text):
        return None
    parts = [re.sub(r"[^\w' ]", "", p).strip() for p in re.split(r"[,.!?;\n]+|\s+and\s+|\s+&\s+", text)]
    found = []
    for p in parts:
        if not p:
            continue
        if not _PART.match(p):
            return None
        found += [k for k, rx in _FIRST.items() if rx.match(p)][:1] or ["thanks"]
        if re.search(KINDS["about"], p):
            found.append("about")
    if not found:
        return None
    # "help" or "what can you do" anywhere wins (the user wants to know what to ask); otherwise the first part
    return "about" if "about" in found else found[0]


_SALUTE = [(r"namaste|namaskar", "Namaste!"), (r"good morning", "Good morning!"), (r"good afternoon", "Good afternoon!"),
           (r"good evening", "Good evening!"), (r"hello", "Hello!"), (r"hey", "Hey!")]


def reply(message: str, what: str) -> str:
    text = FALLBACK[what]
    if what == "greeting":
        low = (message or "").lower()
        hello = next((h for pat, h in _SALUTE if re.search(pat, low)), "Hi!")
        text = hello + text[len("Hi!"):]
        if re.search(KINDS["how_are_you"], low):
            text = text.replace(" How can I help", " I'm doing well, thanks. How can I help", 1)
    return text


def reply_stream(message: str, history: Optional[List[dict]], what: str) -> Iterator[str]:
    """The reply in a few pieces, so the chat shows it the same way as a written answer."""
    for piece in re.findall(r"\S+\s*", reply(message, what)):
        yield piece
