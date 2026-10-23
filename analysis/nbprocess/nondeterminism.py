import re

NONDETERMINISTIC_PATTERNS = [
    r"\brandom\.",
    r"\bnp\.random",
    r"\bnumpy\.random",
    r"\btime\.time",
    r"\bdatetime\.now",
    r"\bos\.environ",
    r"\buuid\.",
]

def detect_nondeterminism(source):
    return any(re.search(p, source) for p in NONDETERMINISTIC_PATTERNS)
