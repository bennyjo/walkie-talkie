"""Text rules: what of a reply is read aloud, and what of an utterance counts.

The filter rules come from walk mode's `clean_line`, `prose_lines`, `speakable`,
`split_unit`, and `spell_numbers`. Walk mode filters a finished reply. Here the
reply arrives in small pieces, so `SpeechFilter` keeps the same rules as state
and hands out each sentence as soon as it's complete.
"""
import re

MAX_INPUT = 50000   # characters filtered; replies are far shorter
MAX_CHARS = 1200    # about 80 seconds of speech
MAX_UNIT = 250      # longest unit sent to the voice at once
FIRST_UNIT = 60     # the first unit of a reply is cut short, so its first word comes sooner
TRUNCATED = "That's all I'll read out. The rest is on screen."
NOTHING_SPEAKABLE = "This reply is code or links only. It's on screen."
# Where something was left out, so a sentence never reads as if it was whole: "Should I run?" hides what it asks.
LEFT_OUT = "something on screen"
ON_SCREEN = "Something on screen."

FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")
INDENTED = re.compile(r"^(?: {4,}|\t)")
TABLE = re.compile(r"^\s*\||\S\s+\|\s+\S|^\s*:?-{3,}:?\s*\|")
# "$ git status", tracebacks, '2026-10-12T14:52:01Z ERROR ...', and JSON lines.
NOT_PROSE = re.compile(
    r'^\s*(?:\$ |Traceback \(most recent call last\)|File ".*", line \d)'
    r'|^\s*\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}'
    r'|^\s*[{\[]\s*"')
DIFF_RUN = re.compile(r"^(?:[+\-]|@@|diff --git |index [0-9a-f]|\s)")
DIFF_HARD = re.compile(r"^(?:\+|@@|diff --git |--- a/)")
RULE = re.compile(r"\s*([-*_])(\s*\1){2,}\s*")
URL = re.compile(r"<?\b(?:https?|ftp|file|ssh)://[^\s>)]+>?|\bwww\.[^\s)]+", re.I)
HTML = re.compile(r"</?[A-Za-z][\w-]*(?:\s[^<>]*)?/?>|<>")
# These only shape the words around them. Any other tag may be the thing a sentence is about, like <prod-db>.
FORMAT_TAG = re.compile(
    r"</?(?:a|b|i|u|s|em|strong|code|span|p|br|hr|div|ul|ol|li|h[1-6]|blockquote)"
    r"""(?:\s+[\w:-]+=(?:"[^"<>]*"|'[^'<>]*'|[^\s<>"']*))*\s*/?>""", re.I)  # each attribute parses one way only
SECRET = re.compile(
    r"\b(?:sk-[\w-]{6,}|sk_(?:live|test)_\w+|gh[pousr]_\w{6,}|github_pat_\w+|"
    r"glpat-[\w-]+|xox[abposr]-[\w-]+|AKIA[0-9A-Z]{8,}|AIza[\w-]{10,}|"
    r"ya29\.[\w-]+|eyJ[\w-]{10,}\.[\w.-]+)")
ENV_VAR = re.compile(r"\$\{?[A-Za-z_]\w*\}?[\w.@%+=:,~/-]*")
ASSIGNMENT = re.compile(r"(?<![\w-])[A-Za-z_]\w*=\S*")
# /abs/path, ~/path, ./path, ../path (not the "/or" in "and/or")
ABS_PATH = re.compile(r"(?<![\w.~/])(?:~|\.\.?)?/[\w.@%+=:,~-][\w.@%+=:,~/-]*(?<![.,:;])")
# relative paths: a/b.ext or a/b/c. Neither pattern eats a sentence's full
# stop, and starting only after a space keeps long a-b-c runs linear.
REL_PATH = re.compile(r"(?<![\w./-])[\w.-]+(?:/[\w.-]+)+/?(?<![.,:;])")
HASH = re.compile(r"\b(?=[0-9a-fA-F]*\d)(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{7,}\b")
VERSION = re.compile(r"\bv?\d+(?:\.\d+){2,}(?:[-+][\w.]+)?\b")
# Matched only at token starts, then tested, so long a-b-c runs stay linear.
TOKEN = re.compile(r"(?<![\w+/=-])[\w+/=-]{20,}")
LONG_TOKEN = re.compile(r"\S{41,}")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[\"'(]?[A-Z0-9])")
CLAUSE_END = re.compile(r"(?<=[,;:])\s+")


def _rel_path(match):
    token = match.group(0)
    if re.fullmatch(r"[\d/.-]+", token):  # dates and fractions: 12/10, 1/2
        return token
    last = token.rstrip("/").rsplit("/", 1)[-1]
    if token.count("/") >= 2 or re.search(r"\w\.\w+$", last):
        return LEFT_OUT
    return token  # and/or, TCP/IP, client/server


def _token(match):
    token = match.group(0)
    mixed = re.search(r"\d", token) and re.search(r"[A-Za-z]", token)
    return LEFT_OUT if mixed else token  # keys and IDs, not long plain words


def clean_line(line):
    line = re.sub(r"(`+)(?:(?!\1).)+?\1", LEFT_OUT, line)    # inline code
    line = FORMAT_TAG.sub(" ", line)
    line = HTML.sub(LEFT_OUT, line)
    line = re.sub(r"!\[[^\[\]]*\]\([^)]*\)", LEFT_OUT, line)  # images
    line = re.sub(r"\[([^\[\]]+)\]\([^)]*\)", r"\1", line)    # links keep their text
    line = URL.sub(LEFT_OUT, line)
    line = SECRET.sub(LEFT_OUT, line)
    line = ENV_VAR.sub(LEFT_OUT, line)
    line = ABS_PATH.sub(LEFT_OUT, line)
    line = REL_PATH.sub(_rel_path, line)
    line = ASSIGNMENT.sub(LEFT_OUT, line)
    line = HASH.sub(LEFT_OUT, line)
    line = VERSION.sub(LEFT_OUT, line)
    line = TOKEN.sub(_token, line)
    line = LONG_TOKEN.sub(LEFT_OUT, line)
    line = re.sub(r"(?<=[\w.,;:!?])" + LEFT_OUT, " " + LEFT_OUT, line)  # never fused to the words beside it
    line = re.sub(LEFT_OUT + r"(?=\w)", LEFT_OUT + " ", line)
    line = re.sub(r"^\s{0,3}#{1,6}\s+", "", line)             # headings
    line = re.sub(r"^\s*>\s?", "", line)                      # quotes
    line = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line)      # list markers
    line = re.sub(r"(\*\*|__)(.+?)\1", r"\2", line)           # bold
    line = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", line)  # italics
    line = re.sub(r"\(\s*[,;:]?\s*\)", "", line)              # emptied brackets
    line = re.sub(r"\s+", " ", line).strip()
    line = re.sub(r"\s+([,.;:!?])", r"\1", line)
    line = re.sub(r"([,;:])(?=[,.;:!?])", "", line)           # "see , ." -> "see."
    line = re.sub(LEFT_OUT + r"(?:[\s,;:]+" + LEFT_OUT + ")+", LEFT_OUT, line)  # one is enough
    return line


# --- numbers -------------------------------------------------------------------
# Spelled out in English words before the voice sees them, so no engine can
# read a digit with the wrong language's rules.

ONES = ("zero one two three four five six seven eight nine ten eleven twelve "
        "thirteen fourteen fifteen sixteen seventeen eighteen nineteen").split()
TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
SCALES = ["", " thousand", " million", " billion", " trillion"]
ORDINAL = {"one": "first", "two": "second", "three": "third", "five": "fifth",
           "eight": "eighth", "nine": "ninth", "twelve": "twelfth"}
MONTHS = ("January February March April May June July August September "
          "October November December").split()
CURRENCY = {"$": ("dollar", "cent"), "€": ("euro", "cent"), "£": ("pound", "penny")}
AMOUNT = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
SCALE_WORD = {"k": "thousand", "m": "million", "bn": "billion", "b": "billion"}


def _below_1000(n):
    hundreds, rest = divmod(n, 100)
    words = [ONES[hundreds] + " hundred"] if hundreds else []
    if rest >= 20:
        words.append(TENS[rest // 10] + ("-" + ONES[rest % 10] if rest % 10 else ""))
    elif rest or not hundreds:
        words.append(ONES[rest])
    return " ".join(words)


def cardinal(digits):
    """'1528021' -> 'one million five hundred twenty-eight thousand twenty-one'."""
    digits = digits.replace(",", "")
    if len(digits) > 15 or (len(digits) > 1 and digits[0] == "0"):
        return " ".join(ONES[int(d)] for d in digits)  # IDs and codes: digit by digit
    n, groups = int(digits), []
    for scale in SCALES:
        n, group = divmod(n, 1000)
        if group:
            groups.insert(0, _below_1000(group) + scale)
        if not n:
            break
    return " ".join(groups) or "zero"


def number(text):
    """'1,528,021' or '1.6' in words."""
    whole, _, fraction = text.partition(".")
    words = cardinal(whole)
    return words + (" point " + " ".join(ONES[int(d)] for d in fraction) if fraction else "")


def ordinal(digits):
    words = cardinal(digits)
    head, last = re.match(r"(.*?)(\w+)$", words).groups()
    if last in ORDINAL:
        return head + ORDINAL[last]
    return head + (last[:-1] + "ieth" if last.endswith("y") else last + "th")


def _money(m):
    unit, cents = CURRENCY[m.group(1)]
    amount, scale = m.group(2), (m.group(3) or "").lower()
    whole, _, fraction = amount.partition(".")
    scale = SCALE_WORD.get(scale, scale)
    if scale or len(fraction) not in (0, 2):
        return "%s%s %ss" % (number(amount), " " + scale if scale else "", unit)
    parts = [cardinal(whole) + " " + unit + ("" if whole == "1" else "s")]
    if fraction and int(fraction):
        small = "pence" if cents == "penny" and fraction != "01" else \
            cents + ("" if fraction == "01" else "s")
        parts.append("%s %s" % (cardinal(str(int(fraction))), small))
        if not int(whole.replace(",", "")):
            parts.pop(0)  # $0.50: fifty cents
    return " and ".join(parts)


def _date(m):
    year, month, day = (int(g) for g in m.groups())
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return m.group(0).replace("-", " ")
    return "%s %s, %s" % (MONTHS[month - 1], ordinal(str(day)), cardinal(str(year)))


def _time(m):
    hour, minute = m.group(1), m.group(2)
    if minute == "00":
        return cardinal(hour) + " o'clock"
    return cardinal(hour) + (" oh " + ONES[int(minute)] if minute[0] == "0" else " " + cardinal(minute))


def _spaced_number(m):
    """A number in words, set apart from letters it touches: S3, 10ms."""
    s, start, end = m.string, m.start(), m.end()
    return ("" if start == 0 or not s[start - 1].isalpha() else " ") + number(m.group(0)) + \
        ("" if end == len(s) or not s[end].isalpha() else " ")


def spell_numbers(text):
    """Every digit in text, spelled out in English words."""
    text = re.sub(r"\b(\d{4})-(\d{2})-(\d{2})\b", _date, text)
    text = re.sub(r"\b(\d{1,2}):(\d{2})\b", _time, text)
    text = re.sub(r"([$€£])\s?(%s)(?:\s?(k|m|bn|b|thousand|million|billion|trillion)\b)?"
                  % AMOUNT, _money, text, flags=re.I)
    text = re.sub(r"(%s)\s?%%" % AMOUNT, lambda m: number(m.group(1)) + " percent", text)
    text = re.sub(r"\b(\d+)(?:st|nd|rd|th)\b", lambda m: ordinal(m.group(1)), text)
    text = re.sub(r"(?<=\d)\s?[-–]\s?(?=\d)", " to ", text)  # ranges: 3-5
    return re.sub(AMOUNT, _spaced_number, text)


def split_unit(s, limit=MAX_UNIT):
    """Split a sentence longer than limit at commas, then at spaces."""
    if len(s) <= limit:
        return [s]
    units, cur = [], ""
    for piece in re.split(r"(?<=[,;:])\s+|\s+", s):
        if cur and len(cur) + 1 + len(piece) > limit:
            units.append(cur)
            cur = piece
        else:
            cur = (cur + " " + piece).strip()
    return units + ([cur] if cur else [])


def first_clause(s):
    """Cut a reply's first sentence at its first clause, so the voice has less to do at the start."""
    if len(s) <= FIRST_UNIT:
        return [s]
    for match in CLAUSE_END.finditer(s):
        if match.start() >= 12:
            return [s[:match.start()], s[match.end():]]
    head = s[:FIRST_UNIT].rfind(" ")
    return [s[:head], s[head + 1:]] if head > 0 else [s]


class SpeechFilter:
    """One reply's text pieces in, speakable units out, in order, as sentences complete."""

    def __init__(self):
        self.pending = ""
        self.midline = False    # pending continues a line already judged to be prose
        self.fence = None
        self.pem = self.traceback = self.icode = self.in_list = self.in_diff = False
        self.prev_blank = True
        self.seen = 0           # characters received
        self.total = 0          # characters handed out
        self.truncated = False
        self.spoke = False
        self.hidden = False     # something was left out since the last words read aloud

    def feed(self, text):
        if self.seen >= MAX_INPUT:
            return []
        text = text[:MAX_INPUT - self.seen]
        self.seen += len(text)
        self.pending += text
        out = []
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            out.extend(self._complete(line))
        out.extend(self._peel())
        return out

    def finish(self):
        """The reply ended: what's left, or a short notice if nothing could be read."""
        out = self._complete(self.pending)
        self.pending = ""
        if not self.spoke and not self.truncated and self.seen and not out:
            return [NOTHING_SPEAKABLE]
        if self.hidden:
            out.extend(self._speak(ON_SCREEN))
        return out

    def _complete(self, line):
        if self.midline:
            self.midline = False
            return self._units(line)
        if self._judge(line):
            return self._units(line)
        if line.strip() and not RULE.fullmatch(line):
            self.hidden = True  # a code block, a table, or the like
        return []

    def _peel(self):
        """Hand out the finished sentences of a line that hasn't ended yet."""
        if not self.midline and not self._may_be_prose(self.pending):
            return []
        cuts = list(SENTENCE_END.finditer(self.pending))
        for cut in reversed(cuts):
            done = self.pending[:cut.start()]
            if done.count("`") % 2 == 0:  # never inside inline code
                if not self.midline:
                    self._judge(done)
                    self.midline = True
                self.pending = self.pending[cut.end():]
                return self._units(done)
        return []

    def _may_be_prose(self, line):
        """A test of how a line starts that changes nothing."""
        if self.fence or self.pem or self.traceback or not line.strip():
            return False
        if self.in_diff and DIFF_RUN.match(line):
            return False
        return not (FENCE.match(line) or INDENTED.match(line) or NOT_PROSE.match(line)
                    or TABLE.search(line) or DIFF_HARD.match(line) or "-----BEGIN " in line)

    def _judge(self, line):
        """True for a line that's prose: not code, keys, a table, a rule, or a diff."""
        m = FENCE.match(line)
        if self.fence:
            if m and m.group(1)[0] == self.fence[0] and len(m.group(1)) >= len(self.fence):
                self.fence = None
            return False
        if m:
            self.fence = m.group(1)
            return False
        if self.pem or "-----BEGIN " in line:                   # PEM keys
            self.pem = "-----END " not in line
            return False
        if self.traceback:  # its indented frames, then the exception line
            self.traceback = bool(line.strip()) and line[0].isspace()
            return False
        if line.lstrip().startswith("Traceback (most recent call last)"):
            self.traceback = True
            return False
        if not line.strip():
            self.prev_blank = True
            self.in_diff = False
            return False
        indented = INDENTED.match(line)
        if self.icode and indented:
            return False
        self.icode = False
        if indented and self.prev_blank and not self.in_list:   # indented code
            self.icode = True
            return False
        self.prev_blank = False
        if LIST_ITEM.match(line):
            self.in_list = True
        elif not line[0].isspace():
            self.in_list = False
        if DIFF_HARD.match(line):
            self.in_diff = True
            return False
        if self.in_diff:
            if DIFF_RUN.match(line):
                return False
            self.in_diff = False
        return not (NOT_PROSE.match(line) or TABLE.search(line) or RULE.fullmatch(line))

    def _units(self, line):
        out = []
        for sentence in SENTENCE_END.split(clean_line(line)):
            if not re.search(r"[A-Za-z0-9]", sentence.replace(LEFT_OUT, "")):
                self.hidden = self.hidden or LEFT_OUT in sentence
                continue
            if self.hidden:
                out.extend(self._speak(ON_SCREEN))
            out.extend(self._speak(sentence))
        return out

    def _speak(self, sentence):
        self.hidden = False
        out = []
        if self.truncated:
            return out
        spelled = spell_numbers(sentence.strip())
        pieces = first_clause(spelled) if not self.spoke else [spelled]
        for piece in pieces:
            for unit in split_unit(piece):
                if self.truncated:
                    break
                if self.total + len(unit) > MAX_CHARS:
                    self.truncated = True
                    out.append(TRUNCATED)
                    break
                self.spoke = True
                self.total += len(unit) + 1
                out.append(unit)
        return out


# --- utterances ----------------------------------------------------------------

FILLERS = {"ah", "eh", "er", "erm", "hm", "hmm", "mhm", "mm", "oh", "uh", "um", "umm"}
# Words a thought rarely ends on. The transcriber's punctuation can't say, because it adds a full stop to a fragment.
TRAILING = {
    "and", "so", "but", "or", "because", "then", "like", "if", "when", "while", "although", "though", "than", "as",
    "um", "uh", "er", "erm", "the", "a", "an", "my", "your", "our", "their",
    "to", "of", "in", "on", "at", "for", "with", "from", "by", "about", "into", "over", "through",
    "should", "could", "would", "will", "can", "must", "might", "may", "shall", "need", "want",
    "is", "are", "was", "were", "be", "been", "have", "has", "had", "do", "does", "did", "not",
    "i", "we", "they", "he", "she", "also", "maybe",
}
CANCEL = {"cancel", "never mind cancel", "nevermind cancel"}
UNSURE_YES = re.compile("(?:\\?|-|\u2013|\u2014|\u2026|\\.\\.\\.)\\s*$")  # "Yes?" asks, and "No -" was going to say more
CONFIRM_TYPING = {"type it", "type that", "yes type it", "yes type that"}
PAUSE = {"pause", "pause it", "paused"}  # the transcriber often hears a lone "pause" as "Pause it." or "Paused."
RESUME = {"go", "play", "start"}  # these mean something only while paused


def words_of(text):
    return re.findall(r"[a-z0-9']+", text.lower())


def trails_off(text):
    """True when someone stopped on a word that usually has more after it."""
    words = words_of(text)
    return bool(words) and words[-1] in TRAILING


def spoken_words(text):
    return [w for w in words_of(text) if w not in FILLERS]


def kind(text):
    """What an utterance asks for: noise, cancel, confirm-typing, yes, no, pause, resume, or prompt."""
    words = words_of(text)
    if len("".join(words)) < 2 or all(w in FILLERS for w in words):
        return "noise"
    spoken = " ".join(spoken_words(text))
    if spoken in CANCEL:
        return "cancel"
    if spoken in CONFIRM_TYPING:
        return "confirm-typing"
    if spoken in ("yes", "no"):
        return "prompt" if UNSURE_YES.search(text) else spoken
    if spoken in PAUSE:
        return "pause"
    if spoken in RESUME:
        return "resume"
    return "prompt"


def sanitize_prompt(text):
    """One line of printable text, never a slash command, shell command, or memory."""
    text = "".join(c for c in " ".join(text.split()) if c.isprintable())
    return text.lstrip("/!#@ ")
