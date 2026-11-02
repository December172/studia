"""Display verified exam-list fields; never infer grades from history order."""
from decimal import Decimal, InvalidOperation


def nonnegative_number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() and number >= 0 else None


def score_text(item):
    if "score" not in item:
        return "?"
    if item["score"] in (None, ""):
        return "待批" if str(item.get("status")) == "1" else "--"
    value = nonnegative_number(item["score"])
    if value is None:
        return "?"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def completion_with_score(item, status):
    """A MOOC row with a score (or 待批) has been handed in, whatever its answer record says.

    The record check only knows code 2 (submitted) and 0 (draft); a graded paper whose record
    carries another code came back "unknown" and was offered for answering again.
    """
    if status in ("unknown", "not_started") and score_text(item) not in ("?", "--"):
        return "submitted"
    return status


def attempts_text(item, category=None):
    # The live homework catalog uses -1 when there is no retry opportunity.
    homework = category == 1
    if homework and str(item.get("answerCount")) == "-1":
        return "x"
    # The official list displays answerCount/frequency as remaining/total.
    # answerCount is NOT the number already attempted; do not subtract it.
    limit = nonnegative_number(item.get("frequency"))
    if limit is None or limit != int(limit):
        return "?"
    if limit == 0:
        return "不限"
    remaining = nonnegative_number(item.get("answerCount"))
    if remaining is None or remaining != int(remaining) or remaining > limit:
        return "?"
    if homework and remaining == 0:
        return "x"
    return str(int(remaining))
