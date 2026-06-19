import re

_RE = re.compile(r"^[<\[{]?\s*clawchat:(no-reply|silent)\s*/?\s*[>\]}]?$")


def is_no_reply_token(text: str) -> bool:
    return bool(_RE.match(text.strip().lower()))
